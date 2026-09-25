"""
swarm_engine/cognition/compound_semantics.py

Phase 11: language -> decomposition integration.

A compound sentence ("clause and clause and ...") is segmented by ONE
general rule (split on " and "), each clause is interpreted by a trained
RelationalLexicon, and the per-clause meanings are lifted into a native
SemanticStructure IR. Downstream decomposition consumes the STRUCTURE,
not the raw sentence; sub-tasks are derived FROM the structure.

Pipeline (all fail closed):
  1. Segment: re.split(r"\\s+and\\s+", ...) -- the single general rule.
     Empty sentence or any empty clause -> None.
  2. Interpret each clause via lexicon.interpret(clause, entities, probes).
     Any clause -> None -> the whole compound -> None.
  3. Build a SemanticStructure: COMPOUND root, one FACT node per clause
     (attributes: predicate, arg0, arg1 -- canonical roles, never English),
     CONJUNCT edges root->fact, AGENT/PATIENT edges fact->entity nodes.
  4. Derive sub-tasks FROM THE STRUCTURE: one ClauseTask per FACT node,
     in structure order. The clause count and clause contents come from
     the sentence's own segmentation+interpretation; the caller never
     supplies a count.
  5. verify_compound: re-check every clause's fact against its own probes
     and answer cross-clause queries through answer().

Anti-simulation: the module never names verbs, predicates, or entities.
Every clause's meaning comes from the lexicon's learned patterns; training
pairs come from RelationalLexicon.discover (probes are behavioral evidence
authored from the world). No hardcoded semantic answers, no per-sentence
routes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from swarm_engine.acquisition.semantic_structure import (
    SemanticStructure,
    build_structure,
)
from swarm_engine.cognition.relational_lexicon import (
    RelFact,
    RelationalLexicon,
)

# The single general segmentation rule: split on " and ".
# One rule, not per-sentence; case-insensitive.
_CLAUSE_SPLIT = re.compile(r"\s+and\s+", re.IGNORECASE)
# A dangling conjunction ("... and" / "and ...") means an empty clause.
_DANGLING_AND = re.compile(r"(?i)^and\b|\band$")

Probe = Tuple[str, str]


@dataclass(frozen=True)
class ClauseTask:
    """One decomposed sub-task: the clause's own text, its verified RelFact,
    and the behavioral probes that confirm it."""
    clause_text: str
    fact: RelFact
    probes: Tuple[Probe, ...]


@dataclass(frozen=True)
class CompoundInterpretation:
    """The re-assembled whole: per-clause facts, the native IR that
    downstream decomposition consumes, and the sub-tasks derived FROM
    that IR."""
    sentence: str
    clauses: Tuple[RelFact, ...]
    structure: SemanticStructure
    sub_tasks: Tuple[ClauseTask, ...]


# ---------------------------------------------------------------------------
# Segmentation (the one general rule)
# ---------------------------------------------------------------------------

def segment_clauses(sentence: str) -> Optional[List[str]]:
    """Split a compound sentence into clauses on ' and '.

    Returns None if the sentence is empty or any resulting clause is
    empty (fail closed)."""
    if not sentence or not sentence.strip():
        return None
    s = sentence.strip()
    if _DANGLING_AND.search(s):
        return None  # dangling "and" == an empty clause
    parts = [p.strip() for p in _CLAUSE_SPLIT.split(s)]
    if any(not p for p in parts):
        return None
    return parts


def _clause_inputs(sentence: str, clause_probes
                   ) -> Optional[Tuple[List[str], List[Tuple[Probe, ...]]]]:
    """Segment the sentence and align the per-clause probe lists.

    Returns (clause_texts, probe_lists) or None when the sentence is
    unsegmentable or the probe list does not align one-to-one with the
    clauses (fail closed; the test never supplies a clause count)."""
    clause_texts = segment_clauses(sentence)
    if clause_texts is None:
        return None
    if clause_probes is None:
        probe_lists: List[Tuple[Probe, ...]] = [() for _ in clause_texts]
    else:
        if len(clause_probes) != len(clause_texts):
            return None
        probe_lists = [tuple(p) for p in clause_probes]
    return clause_texts, probe_lists


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------

def interpret_each_clause(
        lexicon: RelationalLexicon,
        sentence: str,
        candidate_entities: Sequence[str],
        clause_probes: Optional[Sequence[Sequence[Probe]]],
) -> Optional[List[Tuple[str, Optional[RelFact]]]]:
    """Segment and interpret each clause independently.

    Returns [(clause_text, fact_or_None), ...] -- the per-clause results
    needed for failure attribution (which clause failed). Returns None
    when the sentence is unsegmentable or probes misalign."""
    inputs = _clause_inputs(sentence, clause_probes)
    if inputs is None:
        return None
    clause_texts, probe_lists = inputs
    out: List[Tuple[str, Optional[RelFact]]] = []
    for text, probes in zip(clause_texts, probe_lists):
        fact = lexicon.interpret(text, candidate_entities, list(probes))
        out.append((text, fact))
    return out


def _build_compound_structure(clauses: Sequence[RelFact]) -> SemanticStructure:
    """Lift verified per-clause RelFacts into a native SemanticStructure.

    COMPOUND root, one FACT node per clause (attributes: predicate, arg0,
    arg1 -- canonical roles, never English), CONJUNCT edges root->fact,
    AGENT/PATIENT edges fact->entity nodes (entity nodes deduped)."""
    nodes: List[dict] = [{"node_id": "compound:0", "role": "COMPOUND",
                          "attributes": {"n_clauses": len(clauses)}}]
    edges: List[dict] = []
    entity_ids: dict = {}
    for i, fact in enumerate(clauses):
        fid = f"fact:{i}"
        nodes.append({
            "node_id": fid,
            "role": "FACT",
            "attributes": {
                "predicate": fact.predicate,
                "arg0": fact.arg0,
                "arg1": fact.arg1,
            },
        })
        edges.append({"source": "compound:0", "relation": "CONJUNCT",
                      "target": fid})
        for role, name in (("AGENT", fact.arg0), ("PATIENT", fact.arg1)):
            eid = entity_ids.get(name)
            if eid is None:
                eid = f"entity:{name}"
                entity_ids[name] = eid
                nodes.append({"node_id": eid, "role": "ENTITY",
                              "attributes": {"name": name}})
            edges.append({"source": fid, "relation": role, "target": eid})
    return build_structure(
        nodes=nodes,
        edges=edges,
        provenance={
            "builder": "compound_semantics.v1",
            "n_clauses": len(clauses),
        },
    )


def _derive_sub_tasks(structure: SemanticStructure,
                      clause_texts: Sequence[str],
                      probe_lists: Sequence[Sequence[Probe]]
                      ) -> Tuple[ClauseTask, ...]:
    """Derive sub-tasks FROM THE STRUCTURE: one ClauseTask per FACT node,
    in structure order. Facts are reconstructed from the FACT nodes'
    predicate/arg0/arg1 attributes; the caller-supplied ordered lists only
    contribute the clause's own text and probes (aligned by position)."""
    fact_nodes = [n for n in structure.nodes if n.role == "FACT"]
    tasks: List[ClauseTask] = []
    for k, node in enumerate(fact_nodes):
        attrs = dict(node.attributes)
        fact = RelFact(attrs["predicate"], attrs["arg0"], attrs["arg1"])
        tasks.append(ClauseTask(
            clause_text=clause_texts[k],
            fact=fact,
            probes=tuple(probe_lists[k]),
        ))
    return tuple(tasks)


def interpret_compound(
        lexicon: RelationalLexicon,
        sentence: str,
        candidate_entities: Sequence[str],
        clause_probes: Optional[Sequence[Sequence[Probe]]],
) -> Optional[CompoundInterpretation]:
    """Interpret a compound sentence into a structured decomposition.

    Fail closed: unsegmentable sentence, probe/clause misalignment,
    unknown clause pattern, ambiguous clause, or any probe failure in
    any clause -> None."""
    inputs = _clause_inputs(sentence, clause_probes)
    if inputs is None:
        return None
    clause_texts, probe_lists = inputs
    clauses: List[RelFact] = []
    for text, probes in zip(clause_texts, probe_lists):
        fact = lexicon.interpret(text, candidate_entities, list(probes))
        if fact is None:
            return None  # one bad clause kills the whole compound
        clauses.append(fact)
    structure = _build_compound_structure(clauses)
    sub_tasks = _derive_sub_tasks(structure, clause_texts, probe_lists)
    return CompoundInterpretation(
        sentence=sentence,
        clauses=tuple(clauses),
        structure=structure,
        sub_tasks=sub_tasks,
    )


# ---------------------------------------------------------------------------
# Reassembly: query the decomposed whole
# ---------------------------------------------------------------------------

def answer(comp: CompoundInterpretation, role: str,
           entity: str) -> Optional[str]:
    """Cross-clause query over the re-assembled compound.

    role="agent": return arg0 of the clause whose arg1 == entity
        (the supporter of `entity`).
    role="patient": return arg1 of the clause whose arg0 == entity
        (what `entity` supports).
    Returns None when no clause matches (or role is unknown)."""
    role = (role or "").lower()
    entity = (entity or "").lower()
    for fact in comp.clauses:
        if role == "agent" and fact.arg1.lower() == entity:
            return fact.arg0
        if role == "patient" and fact.arg0.lower() == entity:
            return fact.arg1
    return None


def verify_compound(comp: CompoundInterpretation,
                    cross_probes: Sequence[Tuple[str, str, str]] = ()
                    ) -> bool:
    """Re-verify the compound: every clause's fact must still satisfy its
    own probes (same probe semantics as RelationalLexicon), AND the union
    of clauses must answer every cross-clause probe
    (role, entity, expected) via answer()."""
    for task in comp.sub_tasks:
        for kind, expected in task.probes:
            if not RelationalLexicon._probe_ok(task.fact, kind, expected):
                return False
    for role, entity, expected in cross_probes:
        got = answer(comp, role, entity)
        if got is None or got.lower() != expected.lower():
            return False
    return True
