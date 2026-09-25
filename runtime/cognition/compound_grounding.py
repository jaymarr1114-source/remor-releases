"""
swarm_engine/cognition/compound_grounding.py

Compositional grounding: derive ONE joint acquisition objective from a
compound sentence's semantic STRUCTURE.

The precise gap this closes: single-predicate grounding (grounding_lexicon)
grounds one verified RelFact at a time. A compound sentence ("A verbs B and
B verbs C") denotes a JOINT computation whose wiring -- which clauses share
which entities in which roles -- is stated by the semantic structure, not by
any developer-authored combination formula. This module derives that wiring
from the structure and produces one joint acquisition objective the
orchestrator can consume.

The composition rule (derived from the SemanticStructure, never
test-supplied, never a manual combination formula):
  1. Entity slots: walk the FACT nodes in structure order; for each clause
     take its AGENT edge target then its PATIENT edge target; assign
     positional slots s0, s1, ... in order of first appearance. Entity nodes
     are deduped in the structure, so an entity shared across clauses (blue
     = patient of clause 1, agent of clause 2) occupies ONE slot. The
     shared-slot map IS the dependency graph: which clauses observe which
     slots. (In this world's static physics the clauses are parallel
     computations over shared observations -- fan-out, not a feed chain;
     recursive outcome-feeding is a separate, representationally bounded
     question.)
  2. Clause signature: per clause, (predicate_token, agent_slot,
     patient_slot). Every training compound must share the IDENTICAL clause
     signature -- same clause count, same predicate sequence, same
     slot-sharing pattern -- else None. A structural check, never a
     per-combination branch.
  3. Every clause's predicate token must resolve to a usable GroundingLexicon
     entry via the opaque token lookup; else None (fail closed).
  4. One joint example per compound: inputs are SLOT-keyed
     ({s{i}_{prop}} -- positional, never entity-named, exactly as the
     single-predicate case is role-keyed); the output is the tuple of
     per-clause grounded outcomes, each produced by the LEARNED
     per-predicate program bound through the clause's (agent_slot,
     patient_slot) via the lexicon's own public ground() path. The
     composition is OF groundings; this module never sees world laws.
  5. The objective is a CapabilityRequirement: description = the compound
     sentence under test (caller-supplied text, like make_objective's
     clause_text), examples = joint (slot-keyed inputs, tuple outputs),
     param_names = the slot-keyed schema, origin records the derivation.

Anti-simulation: the module never names a predicate, verb, entity, or law.
Predicate tokens are opaque; entity names only ever flow through the
observation callback into positional slots. Clause order, slot assignment,
and the dependency graph all come from the structure. There is no
RelFact->program table and no test-side execution order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from swarm_engine.acquisition.pipeline import (
    CapabilityRequirement as AcquisitionRequirement,
)
from swarm_engine.cognition.compound_semantics import CompoundInterpretation
from swarm_engine.cognition.grounding_lexicon import GroundingLexicon
from swarm_engine.cognition.relational_lexicon import RelFact

# Minimum joint examples for a learnable objective (same precedent as
# nested_decomposition: fewer than 2 examples cannot ground a joint claim).
MIN_JOINT_EXAMPLES = 2


@dataclass
class ClauseWiring:
    """One clause's structure-derived wiring: opaque predicate token and the
    positional slots its agent / patient occupy."""
    predicate: str
    agent_slot: int
    patient_slot: int
    agent_name: str
    patient_name: str


@dataclass
class CompoundObjective:
    """The derived joint acquisition objective."""
    description: str
    requirement: AcquisitionRequirement
    clause_signature: Tuple[Tuple[str, int, int], ...]
    n_slots: int
    wirings: Tuple[ClauseWiring, ...] = ()
    # dependency graph: slot -> sorted clause indices observing it
    shared_slots: Dict[int, List[int]] = field(default_factory=dict)


def _fact_wirings(comp: CompoundInterpretation
                  ) -> Optional[Tuple[ClauseWiring, ...]]:
    """Read per-clause (predicate, agent, patient) FROM THE STRUCTURE:
    FACT nodes in structure order, AGENT/PATIENT edges to ENTITY nodes.
    Returns None when the structure does not carry the expected shape."""
    try:
        nodes = {n.node_id: n for n in comp.structure.nodes}
        entity_name = {}
        for n in comp.structure.nodes:
            if n.role == "ENTITY":
                attrs = dict(n.attributes)
                if "name" in attrs:
                    entity_name[n.node_id] = attrs["name"]
        edges: Dict[str, Dict[str, str]] = {}
        for e in comp.structure.edges:
            if e.relation in ("AGENT", "PATIENT"):
                edges.setdefault(e.source, {})[e.relation] = e.target
        wirings: List[ClauseWiring] = []
        for n in comp.structure.nodes:
            if n.role != "FACT":
                continue
            attrs = dict(n.attributes)
            pred = attrs.get("predicate")
            rel = edges.get(n.node_id, {})
            a_id, p_id = rel.get("AGENT"), rel.get("PATIENT")
            if not pred or a_id not in entity_name or p_id not in entity_name:
                return None
            # slot assignment is positional by first appearance; entity
            # nodes are deduped in the structure, so a shared entity maps
            # to one slot no matter how many clauses mention it.
            wirings.append(ClauseWiring(
                predicate=str(pred), agent_slot=-1, patient_slot=-1,
                agent_name=entity_name[a_id], patient_name=entity_name[p_id]))
        if not wirings:
            return None
        slot_of: Dict[str, int] = {}
        for w in wirings:
            for name in (w.agent_name, w.patient_name):
                if name not in slot_of:
                    slot_of[name] = len(slot_of)
            w.agent_slot = slot_of[w.agent_name]
            w.patient_slot = slot_of[w.patient_name]
        return tuple(wirings)
    except Exception:
        return None


class _SlotWorld:
    """Minimal sensor adapter: exposes entity observations through the
    observe(name) interface GroundingLexicon.ground() expects."""

    def __init__(self, observations: Dict[str, Dict[str, Any]]) -> None:
        self._obs = observations

    def observe(self, name: str) -> Optional[Dict[str, Any]]:
        obs = self._obs.get((name or "").lower())
        return dict(obs) if obs is not None else None


def compose_compound_objective(
        compounds: Sequence[CompoundInterpretation],
        lexicon: GroundingLexicon,
        observe: Callable[[str], Optional[Dict[str, Any]]],
        description: str = "",
) -> Optional[CompoundObjective]:
    """Derive one joint acquisition objective from compound structures.

    `compounds`: training compounds sharing one clause signature (checked
      structurally, not per-combination).
    `lexicon`: the learned per-predicate groundings (opaque token lookup).
    `observe`: the sensor interface (entity name -> property dict); the
      module never imports or names the world behind it.
    `description`: the compound sentence under test (caller-supplied text).

    Fail closed (-> None): no compounds, any compound None, unparseable
    structure, differing clause signatures, unknown/unusable predicate,
    unobservable entity, non-numeric outcome, fewer than
    MIN_JOINT_EXAMPLES usable joint examples.
    """
    compounds = [c for c in compounds if c is not None]
    if not compounds:
        return None

    wirings_per_compound: List[Tuple[ClauseWiring, ...]] = []
    for comp in compounds:
        w = _fact_wirings(comp)
        if w is None:
            return None
        wirings_per_compound.append(w)

    signatures = [tuple((w.predicate, w.agent_slot, w.patient_slot)
                        for w in wset)
                  for wset in wirings_per_compound]
    if any(sig != signatures[0] for sig in signatures):
        return None  # not one joint computation: structural mismatch
    signature = signatures[0]
    wirings = wirings_per_compound[0]
    n_slots = max(max(w.agent_slot, w.patient_slot) for w in wirings) + 1

    # Every predicate token must ground through the learned lexicon
    # (opaque lookup; fail closed on unknown/conflicted/corrupt).
    for w in wirings:
        entry = lexicon.get(w.predicate)
        if entry is None or not entry.usable():
            return None

    # The shared-slot dependency graph, derived from the wirings.
    shared: Dict[int, List[int]] = {}
    for k, w in enumerate(wirings):
        for s in (w.agent_slot, w.patient_slot):
            shared.setdefault(s, []).append(k)
    shared = {s: sorted(ks) for s, ks in sorted(shared.items())}

    examples: List[Tuple[Dict[str, Any], Tuple[Any, ...]]] = []
    prop_vocab: List[str] = []
    for comp, wset in zip(compounds, wirings_per_compound):
        # One joint observation per distinct entity in this compound.
        names: List[str] = []
        for w in wset:
            for nm in (w.agent_name, w.patient_name):
                if nm not in names:
                    names.append(nm)
        obs_by_name: Dict[str, Dict[str, Any]] = {}
        ok = True
        for nm in names:
            try:
                obs = observe(nm)
            except Exception:
                obs = None
            if not obs:
                ok = False
                break
            obs_by_name[nm.lower()] = dict(obs)
        if not ok:
            continue
        if not prop_vocab:
            prop_vocab = sorted({p for o in obs_by_name.values()
                                 for p in o.keys()})
        slot_obs: List[Dict[str, Any]] = []
        for w in wset:
            for nm, slot in ((w.agent_name, w.agent_slot),
                             (w.patient_name, w.patient_slot)):
                while len(slot_obs) <= slot:
                    slot_obs.append({})
                if not slot_obs[slot]:
                    slot_obs[slot] = obs_by_name[nm.lower()]
        # Per-clause outcomes from the LEARNED groundings (composition OF
        # groundings, via the lexicon's public fail-closed path).
        outcomes: List[Any] = []
        slot_world = _SlotWorld(obs_by_name)
        for w in wset:
            r = lexicon.ground(
                RelFact(w.predicate, w.agent_name, w.patient_name),
                slot_world)
            if r is None or not isinstance(r.predicted, (int, float)):
                ok = False
                break
            outcomes.append(r.predicted)
        if not ok:
            continue
        inputs: Dict[str, Any] = {}
        for i, obs in enumerate(slot_obs):
            for prop in prop_vocab:
                if prop not in obs:
                    ok = False
                    break
                inputs[f"s{i}_{prop}"] = obs[prop]
            if not ok:
                break
        if not ok:
            continue
        examples.append((inputs, tuple(outcomes)))

    if len(examples) < MIN_JOINT_EXAMPLES:
        return None
    schema = tuple(f"s{i}_{prop}" for i in range(n_slots)
                   for prop in prop_vocab)
    if any(tuple(sorted(inp.keys())) != schema for inp, _ in examples):
        return None

    requirement = AcquisitionRequirement(
        name=f"compound:{len(wirings)}clauses:{n_slots}slots",
        description=description or compounds[0].sentence,
        examples=examples,
        param_names=schema,
        origin={"clause_signature": [list(t) for t in signature],
                "n_slots": n_slots,
                "shared_slots": {str(s): ks
                                 for s, ks in shared.items()},
                "source": "compound_grounding.compose_compound_objective"})
    return CompoundObjective(
        description=requirement.description,
        requirement=requirement,
        clause_signature=signature,
        n_slots=n_slots,
        wirings=wirings,
        shared_slots=shared)
