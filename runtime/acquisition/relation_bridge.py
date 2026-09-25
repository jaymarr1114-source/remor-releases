"""
swarm_engine/acquisition/relation_bridge.py

M+28.28 — requirements -> relationship hypotheses -> dependency graph.

Builds directly on M+28.27's DiscoveredRequirement objects. Does not add a
second decomposition mechanism; consumes the requirement list M+28.27
already produces and looks for GENERIC structural evidence that one
requirement's clause refers back to another's.

Two evidence signals, neither of which encodes what any clause MEANS:

1. Anaphora (pronoun back-reference). A small, closed, genuinely
   grammatical class of English pronouns is used to detect a clause
   grammatically referring to something introduced earlier — the same
   distinction any anaphora-resolution component would use. Singular
   pronouns (it/its/this/that) are evidence of reference to the single
   most recent prior clause; plural/dual pronouns (they/their/them/both/
   these/those/same) are evidence of reference to more than one prior
   clause, which is the generic evidence a fan-in structure needs. This
   is grammar, not a workflow table: the word "then" is deliberately NOT
   in this list, and no relation type is assigned from any content word.

2. Literal content-word repetition. If a genuine content word introduced
   by clause A's IR reappears in clause B's IR (B after A), that is
   evidence A and B relate — either A produces something B needs
   (PRODUCES_INPUT_FOR) or A and B both draw on a term neither of them
   introduces fresh (SHARES_SOURCE_WITH, evidence of a shared prerequisite
   rather than a dependency between A and B themselves).

Neither signal is keyed on what the shared/referenced word actually is;
the same code path fires whether the shared word is "total", "ticker", or
"batter". No table maps any specific word to any specific relation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from swarm_engine.acquisition.objective_bridge import DiscoveredRequirement


# A closed, genuinely grammatical class -- pronouns and generic
# cross-reference markers, not domain vocabulary. "then"/"after"/"before"
# are deliberately excluded: this file is explicitly forbidden from
# treating sequence words as dependency evidence.
_SINGULAR_ANAPHORA = {"it", "its", "this", "that"}
_PLURAL_ANAPHORA = {"they", "their", "them", "both", "these", "those", "same"}

# Generic function words to exclude from "content word" comparisons, so
# that shared stopwords (the, a, of) never register as evidence. This is
# a closed grammatical class, not a semantic mapping of any kind.
_GENERIC_STOPWORDS = {
    "the", "a", "an", "of", "for", "to", "and", "or", "then", "that",
    "this", "these", "those", "it", "its", "they", "their", "them",
    "is", "are", "be", "with", "into", "from", "on", "in", "as", "by",
    "whether", "if", "each", "any", "all", "both", "same", "given",
}


class RelationType(Enum):
    DEPENDS_ON = "depends_on"
    PRODUCES_INPUT_FOR = "produces_input_for"
    SHARES_SOURCE_WITH = "shares_source_with"
    INDEPENDENT = "independent"
    CONFLICTS_WITH = "conflicts_with"
    UNKNOWN = "unknown"


@dataclass
class RequirementRelationHypothesis:
    source_requirement: str        # requirement_id
    target_requirement: str        # requirement_id
    relation_type: RelationType
    evidence: str
    confidence: float
    provenance: Dict[str, Any] = field(default_factory=dict)
    status: str = "supported"      # supported|unsupported|contradictory|unknown

    def as_dict(self) -> Dict[str, Any]:
        return {"source_requirement": self.source_requirement,
                "target_requirement": self.target_requirement,
                "relation_type": self.relation_type.value,
                "evidence": self.evidence, "confidence": round(self.confidence, 3),
                "provenance": self.provenance, "status": self.status}


def _definite_reference(req: DiscoveredRequirement, word: str) -> bool:
    """True if word appears immediately preceded by a definite article in
    this clause -- a generic grammatical signal ("the total") that this
    clause is referencing something already established, as opposed to
    introducing it fresh. Never depends on what the word itself is."""
    tokens = [w.strip(".,;:!?") for w in req.description.lower().split()]
    for i, t in enumerate(tokens):
        if t == word and i > 0 and tokens[i - 1] in ("the",):
            return True
    return False


def _content_words(req: DiscoveredRequirement) -> Set[str]:
    words = set()
    for w in req.description.lower().split():
        w = w.strip(".,;:!?")
        if w and w not in _GENERIC_STOPWORDS and len(w) > 2:
            words.add(w)
    return words


def _anaphora_class(req: DiscoveredRequirement) -> Optional[str]:
    words = {w.strip(".,;:!?") for w in req.description.lower().split()}
    if words & _PLURAL_ANAPHORA:
        return "plural"
    if words & _SINGULAR_ANAPHORA:
        return "singular"
    return None


def infer_relations(requirements: List[DiscoveredRequirement]
                    ) -> List[RequirementRelationHypothesis]:
    """Generic pairwise evidence evaluation over discovered requirements.
    Text order is used only to decide which clause could plausibly refer
    BACK to which (a requirement cannot anaphorically refer to a clause
    that hasn't been stated yet) — it is never itself treated as
    dependency evidence on its own (a plain unlinked pair stays
    INDEPENDENT even though one appears after the other in the text).
    """
    hyps: List[RequirementRelationHypothesis] = []
    n = len(requirements)
    content = [_content_words(r) for r in requirements]
    anaphora_class = [_anaphora_class(r) for r in requirements]

    # Resolve each clause's singular-anaphora antecedent by chasing back
    # through any chain of preceding clauses that ALSO introduce no new
    # referent of their own (i.e. are themselves pronoun-only clauses) --
    # a generic chain-following rule, not specific to any word or domain.
    resolved_antecedent: List[Optional[int]] = [None] * n
    for j in range(n):
        if anaphora_class[j] == "singular" and j >= 1:
            candidate = j - 1
            seen = set()
            while (anaphora_class[candidate] == "singular"
                  and resolved_antecedent[candidate] is not None
                  and candidate not in seen):
                seen.add(candidate)
                candidate = resolved_antecedent[candidate]
            resolved_antecedent[j] = candidate

    for j in range(n):
        target = requirements[j]
        anaphora = anaphora_class[j]

        if anaphora == "singular" and resolved_antecedent[j] is not None:
            src = requirements[resolved_antecedent[j]]
            hyps.append(RequirementRelationHypothesis(
                source_requirement=src.requirement_id,
                target_requirement=target.requirement_id,
                relation_type=RelationType.PRODUCES_INPUT_FOR,
                evidence=f"{target.description!r} contains a singular "
                         f"back-reference, resolved (chasing any "
                         f"intervening pronoun-only clauses) to "
                         f"{src.description!r}",
                confidence=0.7,
                provenance={"signal": "singular_anaphora",
                            "chained": resolved_antecedent[j] != j - 1}))
        elif anaphora == "plural" and j >= 2:
            for k in (j - 2, j - 1):
                src = requirements[k]
                hyps.append(RequirementRelationHypothesis(
                    source_requirement=src.requirement_id,
                    target_requirement=target.requirement_id,
                    relation_type=RelationType.PRODUCES_INPUT_FOR,
                    evidence=f"{target.description!r} contains a plural/"
                             f"dual back-reference; {src.description!r} "
                             f"is one of the two most recent candidate "
                             f"antecedents",
                    confidence=0.6,
                    provenance={"signal": "plural_anaphora"}))
        elif anaphora == "plural" and j == 1:
            src = requirements[0]
            hyps.append(RequirementRelationHypothesis(
                source_requirement=src.requirement_id,
                target_requirement=target.requirement_id,
                relation_type=RelationType.PRODUCES_INPUT_FOR,
                evidence=f"{target.description!r} contains a plural "
                         f"back-reference with only one prior requirement "
                         f"available as antecedent",
                confidence=0.5,
                provenance={"signal": "plural_anaphora_single_antecedent"}))

        # Literal content-word repetition, independent of anaphora. A
        # word shared by EXACTLY one earlier and one later requirement is
        # treated as direct produces->consumes evidence (i introduces it,
        # j is the only place it recurs). A word that recurs across three
        # or more requirements is common-source evidence instead — it
        # does not by itself imply any ordering between the requirements
        # that mention it, only that they draw on the same referenced
        # concept. Neither branch is keyed on what the word actually is.
        for i in range(j):
            src = requirements[i]
            shared = content[i] & content[j]
            if not shared:
                continue
            already = any(h.source_requirement == src.requirement_id
                        and h.target_requirement == target.requirement_id
                        for h in hyps)
            if already:
                continue
            exclusive_words = {w for w in shared
                               if sum(1 for c in content if w in c) == 2}
            common_words = shared - exclusive_words
            # Among exclusive words, further split by a purely
            # grammatical signal: does the LATER clause reference the
            # word with a definite article while the EARLIER clause does
            # not? That asymmetry (introduced bare, then referenced
            # definitely) is directional produces->consumes evidence.
            # Symmetric phrasing (both clauses reference it the same way,
            # e.g. both say "the input values") is shared-source evidence
            # instead -- neither clause is shown as producing it for the
            # other.
            directional = {w for w in exclusive_words
                          if _definite_reference(target, w)
                          and not _definite_reference(src, w)}
            symmetric = exclusive_words - directional
            common_words = common_words | symmetric
            exclusive_words = directional
            if exclusive_words:
                hyps.append(RequirementRelationHypothesis(
                    source_requirement=src.requirement_id,
                    target_requirement=target.requirement_id,
                    relation_type=RelationType.PRODUCES_INPUT_FOR,
                    evidence=f"content word(s) {sorted(exclusive_words)!r} "
                             f"appear in exactly these two requirements, "
                             f"introduced bare in {src.description!r} and "
                             f"referenced with a definite article in "
                             f"{target.description!r}",
                    confidence=min(0.75, 0.5 + 0.2 * len(exclusive_words)),
                    provenance={"signal": "exclusive_lexical_overlap",
                                "shared": sorted(exclusive_words)}))
            if common_words:
                hyps.append(RequirementRelationHypothesis(
                    source_requirement=src.requirement_id,
                    target_requirement=target.requirement_id,
                    relation_type=RelationType.SHARES_SOURCE_WITH,
                    evidence=f"content word(s) {sorted(common_words)!r} "
                             f"recur across three or more requirements, "
                             f"including {src.description!r} and "
                             f"{target.description!r}",
                    confidence=min(0.65, 0.3 + 0.15 * len(common_words)),
                    provenance={"signal": "common_source_overlap",
                                "shared": sorted(common_words)}))

    # Contradiction detection: if evidence supports a relation in BOTH
    # directions between the same pair, that is not a genuine dependency
    # in either direction — mark both CONFLICTS_WITH rather than letting
    # a cycle through silently.
    pair_index: Dict[Tuple[str, str], List[int]] = {}
    for idx, h in enumerate(hyps):
        pair_index.setdefault((h.source_requirement, h.target_requirement), []).append(idx)
    seen_pairs = set()
    for (s, t) in list(pair_index.keys()):
        if (t, s) in pair_index and (s, t) not in seen_pairs and (t, s) not in seen_pairs:
            seen_pairs.add((s, t)); seen_pairs.add((t, s))
            for idx in pair_index[(s, t)] + pair_index[(t, s)]:
                hyps[idx].relation_type = RelationType.CONFLICTS_WITH
                hyps[idx].status = "contradictory"

    return hyps


@dataclass
class DependencyGraph:
    requirement_ids: List[str]
    edges: List[RequirementRelationHypothesis]   # accepted (supported) only
    rejected: List[RequirementRelationHypothesis]  # below threshold / contradictory
    has_cycle: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"requirement_ids": self.requirement_ids,
                "edges": [e.as_dict() for e in self.edges],
                "rejected": [e.as_dict() for e in self.rejected],
                "has_cycle": self.has_cycle}

    def topological_order(self) -> Optional[List[str]]:
        """Returns None if a cycle is detected among accepted edges,
        rather than silently producing an order that hides the cycle."""
        deps: Dict[str, Set[str]] = {r: set() for r in self.requirement_ids}
        for e in self.edges:
            if e.relation_type in (RelationType.PRODUCES_INPUT_FOR, RelationType.DEPENDS_ON):
                deps.setdefault(e.target_requirement, set()).add(e.source_requirement)
        order: List[str] = []
        visited: Dict[str, int] = {}

        def visit(node: str) -> bool:
            state = visited.get(node, 0)
            if state == 2:
                return True
            if state == 1:
                return False  # cycle
            visited[node] = 1
            for dep in deps.get(node, ()):
                if not visit(dep):
                    return False
            visited[node] = 2
            order.append(node)
            return True

        for r in self.requirement_ids:
            if not visit(r):
                self.has_cycle = True
                return None
        return order


def build_dependency_graph(requirements: List[DiscoveredRequirement],
                            confidence_threshold: float = 0.5) -> DependencyGraph:
    hyps = infer_relations(requirements)
    accepted = [h for h in hyps if h.status == "supported"
                and h.confidence >= confidence_threshold
                and h.relation_type != RelationType.CONFLICTS_WITH]
    rejected = [h for h in hyps if h not in accepted]
    graph = DependencyGraph(
        requirement_ids=[r.requirement_id for r in requirements],
        edges=accepted, rejected=rejected)
    graph.topological_order()  # populate has_cycle as a side effect
    return graph
