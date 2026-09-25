"""
swarm_engine/acquisition/gap_reasoner.py

Capability-gap reasoning: decompose what a task needs, map it against what the
engine already has, and determine the minimal set of missing pieces — rather
than treating every unmet goal as one opaque acquisition.

The old GapDetector answered "is there a plan for this exact goal?". That is
enough to trigger acquisition but not enough to reason about it: "I cannot do
X" is a dead end unless it becomes "X needs A, B, C; I have A and C; I need B".
This module makes that second sentence a real computation, not a manner of
speaking.

A requirement graph is built by pattern-matching the goal against a small set
of known compound shapes (fetch-then-parse, transcribe-then-translate,
extract-then-analyze) plus whatever the GapDetector's keyword catalogue
already recognises -- and, when the goal's own worked examples are supplied,
by a purely structural decomposition: if every example's output is a uniform
composite (tuple/list of length >= 2, or dict with >= 2 keys), the requirement
splits into one sub-requirement per output element, with the parent depending
on the children and re-assembled by tuple/list/dict construction. That rule
inspects only the SHAPE of the examples, never the goal's words. Each node is
checked against three places a satisfying capability could already live: the primitive registry (built-in), the
acquired-capability registry (grown), and the provenance graph (trusted).
What is left unmatched is the real gap, and it is returned as a dependency
order so acquisition can work bottom-up.

This is honestly scoped: decomposition is pattern-based, not open-ended
natural-language understanding. It recognises the compound shapes it is given
and falls back to the flat single-requirement gap for everything else, and it
says which case it is in rather than pretending both are the same computation.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from swarm_engine.acquisition.pipeline import CapabilityRequirement


class SatisfiedBy(Enum):
    PRIMITIVE = "primitive"          # already in the built-in vocabulary
    ACQUIRED = "acquired"            # grown by a previous acquisition
    COMPOSITION = "composition"      # reachable by combining what exists
    NONE = "none"                    # a genuine gap


@dataclass
class RequirementNode:
    requirement: CapabilityRequirement
    depends_on: List[str] = field(default_factory=list)   # names of prerequisite nodes
    satisfied_by: SatisfiedBy = SatisfiedBy.NONE
    evidence: str = ""
    difficulty: float = 0.5          # 0 = trivial, 1 = hard; heuristic, not measured
    # Gap-A: when this node was satisfied by behavioral discovery of a
    # capability whose executable identity differs from the node's own
    # name (a reworded decomposition re-derives new node names, but the
    # discovered primitive keeps its admitted name), this records the
    # registered primitive name to execute. Consumed by pair-assembly of
    # a decomposition parent via the parent's structural metadata --
    # never derived from goal text.
    discovered_primitive: Optional[str] = None

    @property
    def name(self) -> str:
        return self.requirement.name

    @property
    def is_gap(self) -> bool:
        # COMPOSITION without a bound primitive is still a materialization gap:
        # optimistic template match does not register a callable the parent
        # can invoke. Require acquisition/admission to bind discovered_primitive.
        if self.satisfied_by is SatisfiedBy.NONE:
            return True
        if (self.satisfied_by is SatisfiedBy.COMPOSITION
                and not self.discovered_primitive):
            return True
        return False

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "depends_on": self.depends_on,
                "satisfied_by": self.satisfied_by.value, "evidence": self.evidence,
                "difficulty": round(self.difficulty, 2),
                "description": self.requirement.description,
                "discovered_primitive": self.discovered_primitive}


@dataclass
class RequirementGraph:
    goal: str
    nodes: Dict[str, RequirementNode] = field(default_factory=dict)
    # Per-node worked examples assigned by analyze(): for a structurally
    # decomposed goal these are the reasoner-projected per-part examples
    # (children) and the full composite examples (parent); for the flat
    # path they are whatever the caller supplied per node name.
    node_examples: Dict[str, List[Tuple[Dict[str, Any], Any]]] = field(
        default_factory=dict)

    def gaps(self) -> List[RequirementNode]:
        return [n for n in self.nodes.values() if n.is_gap]

    def acquisition_order(self) -> List[RequirementNode]:
        """Gaps in dependency order: a node's prerequisites come before it.

        This is what turns "I need A, B, C" into something actionable —
        acquiring C before its dependency B would waste an attempt on a
        capability that cannot yet be composed from anything.
        """
        gaps = {n.name: n for n in self.gaps()}
        ordered: List[RequirementNode] = []
        visited: Set[str] = set()

        def visit(name: str, trail: Set[str]) -> None:
            if name in visited or name not in gaps:
                return
            if name in trail:
                return  # cyclic requirement; break rather than hang
            trail = trail | {name}
            for dep in gaps[name].depends_on:
                visit(dep, trail)
            visited.add(name)
            ordered.append(gaps[name])

        for name in gaps:
            visit(name, set())
        return ordered

    def as_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "total_requirements": len(self.nodes),
                "satisfied": len(self.nodes) - len(self.gaps()),
                "gaps": [n.as_dict() for n in self.gaps()],
                "acquisition_order": [n.name for n in self.acquisition_order()],
                "nodes": {k: v.as_dict() for k, v in self.nodes.items()}}


# ---------------------------------------------------------------------------
# COMPOUND SHAPES
# ---------------------------------------------------------------------------
# Each shape recognises a goal pattern and expands it into ordered sub-
# requirements. This is pattern-based decomposition of known compound
# structures, not general task understanding, and is reported as such.

_COMPOUND_SHAPES: List[Tuple[str, List[Tuple[str, str, List[str]]]]] = [
    (r"transcri\w*.*translat|translat\w*.*transcri",
     [("speech_recognition", "convert spoken audio to text", []),
      ("translation", "translate text between languages", ["speech_recognition"])]),
    (r"(download|fetch|retrieve).*(parse|extract|analy)",
     [("web_retrieval", "fetch content from a URL", []),
      ("document_parsing", "parse the retrieved content into structured data",
       ["web_retrieval"])]),
    (r"(image|photo|picture).*(describ|caption|analy)",
     [("image_understanding", "extract information from an image", []),
      ("summarization", "describe the extracted information in text",
       ["image_understanding"])]),
    (r"pdf.*(extract|analy|summar)",
     [("pdf_extraction", "extract text and structure from a PDF", []),
      ("summarization", "summarize the extracted content", ["pdf_extraction"])]),
]


# ---------------------------------------------------------------------------
# STRUCTURE-DRIVEN OUTPUT DECOMPOSITION
# ---------------------------------------------------------------------------
# A generic, goal-text-blind decomposition rule: when the goal's own worked
# examples uniformly produce a composite output (tuple/list with 2+ elements,
# or dict with 2+ keys), the requirement splits into one sub-requirement per
# output element -- all sharing the same inputs -- with the parent depending
# on the children and satisfiable by re-assembly (tuple/list/dict
# construction over the acquired sub-capabilities). The firing condition
# inspects ONLY the shape of the supplied examples: no goal word, keyword,
# or per-objective branch appears anywhere in it. Node NAMES and
# descriptions are derived from the goal text (as everywhere else in this
# module), but they play no role in whether the rule fires.
#
# Fails closed (returns None) for: no/fewer than 2 examples, atomic outputs,
# mixed output kinds, ragged lengths or key sets, nested composite elements
# (single-level decomposition only -- a stated bound, not a TODO), and
# non-dict or inconsistent inputs. A None here means "fall back to the flat
# single-requirement node", exactly like an unmatched keyword shape.

_COMPOSITE_KINDS = (tuple, list, dict)

# Maximum recursion depth for nested structural decomposition. Each level
# strictly reduces the nesting depth of the projected outputs, so this is
# a fail-closed guard against pathological inputs, not a load-bearing
# limit: realistic nested outputs are a handful of levels at most.
_MAX_DECOMP_DEPTH = 8


def _decomposition_base_name(goal: str) -> str:
    """Deterministic node-name stem for a decomposed goal.

    Purely a labelling function: the same goal text always yields the same
    stem, so examples keyed under it and re-analysis of the same goal agree.
    A short stable hash of the full goal text disambiguates goals that
    share a 28-character prefix -- without it two different goals could
    claim the same node names.
    """
    base = re.sub(r"[^a-z0-9]+", "_", (goal or "").lower()).strip("_")
    stem = base[:24].rstrip("_") or "goal"
    digest = hashlib.sha1((goal or "").encode()).hexdigest()[:6]
    return f"{stem}_{digest}"


def _is_decomposition_parent(node: "RequirementNode") -> bool:
    """Whether this node is the parent of a structural output decomposition.

    Keyed on the structural `output_decomposition` constraint the reasoner
    itself attached — never on goal text. Such a parent is satisfiable only
    by a previously admitted capability, its exact registered name, or the
    validated pair-assembly of its children.
    """
    return bool((getattr(node.requirement, "constraints", None) or {}).get(
        "output_decomposition"))


def _is_behavioral_parent(node: "RequirementNode") -> bool:
    """Whether this node is the parent of a behavioral decomposition (P8).

    Keyed on the `behavioral_decomposition` constraint the reasoner itself
    attached — never on goal text. Such a parent is satisfiable only by a
    previously admitted capability, its exact registered name, or the
    validated behavioral assembly of its children (never a fuzzy
    name-similar stranger, never a single child alone, never an
    unvalidated planner template).
    """
    return bool((getattr(node.requirement, "constraints", None) or {}).get(
        "behavioral_decomposition"))


@dataclass
class OutputDecomposition:
    """The structural split of one requirement into per-element parts."""
    kind: str                      # "tuple" | "list" | "dict"
    arity: int                     # number of output parts (>= 2)
    keys: Optional[List[str]]      # sorted output keys for kind == "dict"
    parent_name: str
    parent_description: str
    # (child_name, child_description, projected_examples,
    #  sub_decomposition_or_None) per part. A non-None sub-decomposition
    # means this part's outputs are themselves uniformly composite and were
    # recursively decomposed; its parent_name == child_name, so the part
    # node is the sub-decomposition's assembly parent.
    children: List[Tuple[str, str, List[Tuple[Dict[str, Any], Any]],
                          Optional["OutputDecomposition"]]]
    input_names: List[str]

    @property
    def assembly_constraint(self) -> Dict[str, Any]:
        """Structural re-assembly recipe for the parent's requirement.

        Consumed by the composer's pair-assembly path: which already-
        acquired primitives to call, with what inputs, and how to combine
        their results. Contains no goal text -- only names, arity, and kind.
        `_build_decomposed_graph` may add a `resolved_primitives` map
        (child node name -> discovered registered primitive name) when
        gap analysis behaviorally discovered children admitted under
        older names; still names only, never goal text.
        """
        return {
            "kind": self.kind,
            "arity": self.arity,
            "keys": list(self.keys) if self.keys else None,
            "children": [c[0] for c in self.children],
            "input_names": list(self.input_names),
        }


class CapabilityGapReasoner:
    """Decomposes a goal, checks each part against everything the engine
    already has, and reports the minimal, ordered set of real gaps."""

    def __init__(self, registry, planner, composer, provenance=None,
                 acquired_specs: Optional[Dict[str, Any]] = None,
                 capabilities: Optional[Any] = None,
                 decomposer: Optional[Any] = None):
        self.reg = registry
        self.planner = planner
        self.composer = composer
        self.provenance = provenance
        self.acquired_specs = acquired_specs if acquired_specs is not None else {}
        self.capabilities = capabilities
        # Behavioral decomposition (P8): an injected BehavioralDecomposer.
        # None (default) preserves today's behavior bit-for-bit -- the
        # behavioral insertion in analyze() is skipped entirely.
        self._decomposer = decomposer
        from swarm_engine.acquisition.semantic import SemanticMatcher
        self._matcher = SemanticMatcher(provenance)

    def analyze(self, goal: str, allow_effects: bool = False,
                examples_by_node: Optional[Dict[str, Sequence[Tuple[Dict[str, Any], Any]]]] = None,
                examples: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None
                ) -> RequirementGraph:
        """Decompose `goal` and resolve each node against current capabilities.

        `examples` are the goal's OWN worked examples (input dicts to
        expected outputs). When supplied they are evidence about the goal's
        output structure: uniform composite outputs trigger the structural
        decomposition below, before any keyword shape is consulted --
        evidence outranks vocabulary. Without examples the path is exactly
        the old keyword-shape / flat behaviour.
        """
        graph = RequirementGraph(goal=goal)
        examples_by_node = examples_by_node or {}

        goal_examples = list(examples) if examples else []
        if not goal_examples:
            # Convenience: the goal's examples may be keyed under the
            # deterministic decomposition parent name (derivable from the
            # goal text alone, before any node exists).
            goal_examples = list(examples_by_node.get(
                _decomposition_base_name(goal), []))

        # Behavioral decomposition (P8/P17) BEFORE structural shape-split.
        # List/string/bit/numeric associative folds must own their domains;
        # structural list/tuple element projection would otherwise steal
        # uniform list outputs (e.g. [x,-1]) and block list_concat monoid
        # acquisition. Behavioral is fail-closed (None) when unwired or
        # when no exact fold/nest exists, so structural still handles
        # heterogeneous tuples/dicts behavioral cannot express.
        behavioral = self._match_behavioral_decomposition(goal, goal_examples)
        if behavioral is not None:
            return self._build_behavioral_graph(
                graph, behavioral, goal_examples, allow_effects)

        decomp = self._match_composite_output(goal, goal_examples)
        if decomp is not None:
            return self._build_decomposed_graph(
                graph, decomp, goal_examples, allow_effects)

        shape = self._match_compound_shape(goal)

        if shape is None:
            requirement = self._single_requirement(goal)
            node = RequirementNode(requirement=requirement)
            # The goal's own examples are the flat node's examples: without
            # this fallback they never reach _resolve(), so check #0
            # (find_compatible, behavioral on examples) is blind for atomic
            # goals and a reworded atomic goal re-acquires instead of
            # discovering its recursively-acquired counterpart. Mirrors the
            # goal_examples fallback resolve() already applies when it
            # builds strategy specs.
            node_examples = (list(examples_by_node.get(node.name, []))
                             or goal_examples)
            # The requirement's own description is the canonical key every
            # acquisition path binds under (compose binds spec.description,
            # generate's admission binds spec.description) -- resolving
            # against it, not the raw goal text, is what lets the exact
            # goal-binding fast path in find_compatible() fire for a
            # previously acquired requirement.
            self._resolve(node, requirement.description, allow_effects,
                          examples=node_examples or None)
            graph.nodes[node.name] = node
            graph.node_examples[node.name] = node_examples
            return graph

        for name, description, deps in shape:
            requirement = CapabilityRequirement(name=name, description=description)
            node = RequirementNode(requirement=requirement, depends_on=list(deps))
            # Same goal_examples fallback as the flat path above: a
            # compound-shape node's own examples must reach _resolve().
            node_examples = (list(examples_by_node.get(name, []))
                             or goal_examples)
            self._resolve(node, description, allow_effects,
                          examples=node_examples or None)
            graph.nodes[name] = node
            graph.node_examples[name] = node_examples
        return graph

    # -- structural output decomposition ------------------------------------
    def _match_composite_output(
            self, goal: str,
            examples: Sequence[Tuple[Dict[str, Any], Any]],
            _depth: int = 0, _base: Optional[str] = None,
            ) -> Optional[OutputDecomposition]:
        """Split a requirement by the structure of its worked examples.

        See the module section above for the contract. NOTE: this method
        never inspects the goal's text for keywords -- `goal` is used only
        to derive deterministic node names/descriptions AFTER the
        structure-only firing decision has been made.

        Recursive: when every projected part output is itself a uniform
        composite, the part is decomposed by the same structural rule,
        producing a multi-level acquisition graph. Non-uniform nesting
        (ragged shapes, or a part whose outputs mix composite and atomic
        across examples) fails closed with None.
        """
        if _depth > _MAX_DECOMP_DEPTH:
            return None
        ex = [(a, o) for a, o in (examples or [])]
        if len(ex) < 2:
            return None
        outs = [o for _, o in ex]
        kinds = {type(o) for o in outs}
        if len(kinds) != 1:
            return None  # mixed output kinds: not uniformly projectable
        kind = next(iter(kinds))
        if kind not in _COMPOSITE_KINDS:
            return None  # atomic output: nothing to split

        # Inputs must be uniform dicts: every part is computed from the
        # same named inputs.
        in_keys = []
        for a, _ in ex:
            if not isinstance(a, dict) or not a:
                return None
            in_keys.append(tuple(sorted(a.keys())))
        if any(k != in_keys[0] for k in in_keys):
            return None
        input_names = list(in_keys[0])

        if kind is dict:
            key_sets = [set(o.keys()) for o in outs]
            if any(k != key_sets[0] for k in key_sets):
                return None  # ragged key sets: not projectable
            keys = sorted(key_sets[0], key=str)
            if len(keys) < 2:
                return None
            labels = list(keys)
            project = lambda o, k: o[k]  # noqa: E731
            arity, kind_name = len(keys), "dict"
        else:
            lengths = {len(o) for o in outs}
            if len(lengths) != 1:
                return None  # ragged lengths: not projectable
            arity = next(iter(lengths))
            if arity < 2:
                return None
            keys = None
            labels = [str(i) for i in range(arity)]
            project = lambda o, i: o[i]  # noqa: E731
            kind_name = "tuple" if kind is tuple else "list"

        base = _base if _base is not None else _decomposition_base_name(goal)
        parent_name = base
        parent_description = (
            f"{goal} [{kind_name} assembly of {arity} output parts]")
        children = []
        for i, label in enumerate(labels):
            child_name = f"{base}_p{i}"
            if kind is dict:
                child_desc = (f"{goal} [output part {label!r} of {arity}]")
                proj = [(dict(a), project(o, label)) for a, o in ex]
            else:
                child_desc = (f"{goal} [output part {i + 1} of {arity}]")
                proj = [(dict(a), project(o, i)) for a, o in ex]
            # Nested structure: when all of this part's projected outputs
            # are composite, the nesting is uniform and the part is split
            # by the same rule. The recursion enforces uniform kind and
            # shape; a ragged part makes it return None, which fails this
            # whole level closed. A part whose outputs mix composite and
            # atomic across examples is not projectable: None.
            part_outs = [o for _, o in proj]
            sub: Optional[OutputDecomposition] = None
            if part_outs and all(isinstance(po, _COMPOSITE_KINDS)
                                for po in part_outs):
                sub = self._match_composite_output(
                    goal, proj, _depth=_depth + 1, _base=child_name)
                if sub is None:
                    return None
            elif any(isinstance(po, _COMPOSITE_KINDS) for po in part_outs):
                return None
            children.append((child_name, child_desc, proj, sub))
        return OutputDecomposition(
            kind=kind_name, arity=arity, keys=keys,
            parent_name=parent_name, parent_description=parent_description,
            children=children, input_names=input_names)

    def _build_decomposed_graph(
            self, graph: RequirementGraph, decomp: OutputDecomposition,
            goal_examples: Sequence[Tuple[Dict[str, Any], Any]],
            allow_effects: bool) -> RequirementGraph:
        """Materialise the structural decomposition as requirement nodes.

        Recursive: a child carrying a sub-decomposition materialises its
        entire sub-graph (leaves, then the sub-assembly parent registered
        under the child's name) before the level's parent is built. The
        parent assembles over sub-parents exactly as over leaf children.
        Dependency order stays children-before-parents at every level, so
        the orchestrator's topological acquisition, the run-time
        primitive-visibility record, and transactional rollback all apply
        unchanged at depth -- no per-level machinery.
        """
        for idx, (child_name, child_desc, proj, sub) in enumerate(
                decomp.children):
            if sub is not None:
                # Nested part: the sub-parent IS this child node; build
                # the whole sub-graph under it.
                self._build_decomposed_graph(graph, sub, proj, allow_effects)
            else:
                requirement = CapabilityRequirement(
                    name=child_name, description=child_desc,
                    constraints={"output_decomposition_part": {
                        "parent": decomp.parent_name,
                        "kind": decomp.kind,
                        "index": idx,
                    }})
                node = RequirementNode(requirement=requirement)
                self._resolve(node, child_desc, allow_effects, examples=proj)
                graph.nodes[child_name] = node
                graph.node_examples[child_name] = list(proj)
        parent_requirement = CapabilityRequirement(
            name=decomp.parent_name, description=decomp.parent_description,
            constraints={"output_decomposition": decomp.assembly_constraint})
        parent = RequirementNode(
            requirement=parent_requirement,
            depends_on=[c[0] for c in decomp.children])
        self._resolve(parent, decomp.parent_description, allow_effects,
                      examples=list(goal_examples))
        graph.nodes[decomp.parent_name] = parent
        graph.node_examples[decomp.parent_name] = list(goal_examples)
        # Gap-A: record which registered primitives the discovered
        # children execute as, inside the parent's own structural
        # metadata. A reworded goal decomposes to NEW node names, but a
        # behaviorally-discovered child keeps its ADMITTED primitive
        # name; without this name->name map the parent's pair-assembly
        # would look for primitives under names that were never
        # registered and honestly refuse. Names only -- no goal text
        # enters this map; children resolved by fresh acquisition are
        # absent (they register under their node names). For nested
        # children the sub-parent node lives under the child's name, so
        # this loop is uniform across depths.
        resolved = {}
        for child_name, _, _, _ in decomp.children:
            prim = graph.nodes[child_name].discovered_primitive
            if prim:
                resolved[child_name] = prim
        if resolved:
            parent.requirement.constraints[
                "output_decomposition"]["resolved_primitives"] = resolved
        return graph

    # -- behavioral decomposition (P8) --------------------------------------
    def _match_behavioral_decomposition(
            self, goal: str,
            goal_examples: Sequence[Tuple[Dict[str, Any], Any]]):
        """Thin delegation to the injected BehavioralDecomposer.

        Returns a DecompositionContract or None (fail closed). None when
        unwired -- today's behavior bit-for-bit -- and None when an
        associative-fold contract discovers no capability gap (see
        _all_children_satisfied): a fold whose every child is already
        satisfiable from the current inventory is not a gap, so the
        existing composition route owns the parent and the decomposition
        search is not redundantly invoked. Nest-chain contracts are never
        suppressed on these grounds (the flat path cannot re-discover the
        outer-op chain).
        """
        decomposer = getattr(self, "_decomposer", None)
        if decomposer is None or not goal_examples:
            return None
        try:
            contract = decomposer.decompose(goal, list(goal_examples))
        except Exception:
            # Fail closed: a decomposition search must never break
            # analysis with an exception.
            return None
        if contract is not None and self._all_children_satisfied(contract):
            # Gap-driven fail-closed, associative folds only: the existing
            # composition machinery (multiway probe's fold track, proven
            # through 7-way) demonstrably re-discovers additive folds over
            # acquired leaves, so a fold contract with no capability gap is
            # redundant. Nest chains are NOT suppressed: the flat path
            # cannot reliably re-discover the outer-op chain (measured:
            # -|x-5| with pre-acquired children fails all flat strategies
            # while the contract-guided nest assembly succeeds), so the
            # decomposition pathway stays the owner for nests. Family is
            # structural contract metadata, never goal text.
            #
            # Exception (v21): when any child is behavioral_reuse or
            # behavioral_param, the contract is the only path that wires
            # hierarchical reuse / identity param bindings into assembly.
            # Suppressing would flatten away proven reuse.
            if contract.reconstructor.family == "associative_fold":
                keep = any(
                    getattr(ch, "source", None) in (
                        "behavioral_reuse", "behavioral_param", "behavioral_const", "behavioral_kv")
                    for ch in contract.children)
                if not keep:
                    return None
        return contract

    def _all_children_satisfied(self, contract) -> bool:
        """True when no discovered child is a capability gap.

        Each child is tested with the reasoner's canonical satisfaction
        checks -- the same `_resolve` `_build_behavioral_graph` applies
        to the materialized child nodes -- on a scratch node carrying the
        same requirement shape (name, description, projected examples,
        behavioral-component constraint). Read-only: scratch nodes are
        discarded and the inventory is never mutated. Only when EVERY
        child is already satisfiable (ACQUIRED/PRIMITIVE, or a bound
        composition) is the decomposition gap-free; a single genuine
        gap keeps the contract. An inspection error fails open (False):
        suppressing a decomposition must never be the result of a
        broken check -- the contract then fires exactly as before.

        Recursive (P9): a child with `sub_contract` is a compound residual
        whose satisfaction requires (a) its nested children satisfied and
        (b) a live registered primitive under the child's own name (the
        assembled intermediate). Without (b), quarantining an intermediate
        would leave grandchildren seeming to "cover" the residual and
        incorrectly suppress hierarchical re-acquisition.
        """
        try:
            parent_base = _decomposition_base_name(contract.parent_goal)
            for child in contract.children:
                sub = getattr(child, "sub_contract", None)
                if sub is not None:
                    if not self._all_children_satisfied(sub):
                        return False
                    if self.reg.get(child.name) is None:
                        return False
                    continue
                # Hierarchical reuse: child already realized by a live
                # acquired/promoted primitive (behavioral value match).
                reuse_prim = getattr(child, "reuse_primitive", None)
                if (getattr(child, "source", None) == "behavioral_reuse"
                        and reuse_prim
                        and self.reg.get(reuse_prim) is not None):
                    continue
                # Param-exact: child equals a parent input column; assembly
                # binds via the registry identity primitive -- not a gap.
                if (getattr(child, "source", None) == "behavioral_param"
                        and reuse_prim
                        and self.reg.get("identity") is not None):
                    continue
                # Constant component: inlined at assembly; never a gap.
                if getattr(child, "source", None) == "behavioral_const":
                    continue
                requirement = CapabilityRequirement(
                    name=child.name, description=child.description,
                    constraints={"behavioral_component": {
                        "parent": parent_base,
                        "index": child.index,
                        "family": contract.reconstructor.family,
                    }})
                node = RequirementNode(requirement=requirement)
                self._resolve(node, child.description, False,
                              examples=list(child.projected_examples))
                if node.is_gap:
                    return False
            return True
        except Exception:
            return False

    def _build_behavioral_graph(
            self, graph: RequirementGraph, contract,
            goal_examples: Sequence[Tuple[Dict[str, Any], Any]],
            allow_effects: bool) -> RequirementGraph:
        """Materialise a behavioral DecompositionContract as requirement nodes.

        Mirrors `_build_decomposed_graph`: one child node per decomposed
        component (with its projected examples), then the parent node with
        `depends_on` naming the children and
        `constraints["behavioral_decomposition"]` carrying the contract's
        `assembly_dict()`. Children resolve through the normal `_resolve`
        checks (an already-acquired capability may satisfy a child
        directly); the parent is exempt from fuzzy/template satisfaction
        (see `_is_behavioral_parent`). The Gap-A `resolved_primitives`
        recording loop is mirrored so behaviorally-discovered children
        admitted under older names stay addressable at assembly time.

        Recursive (P9): a child carrying `sub_contract` materialises its
        entire sub-graph first (grandchildren, then the sub-assembly
        parent registered under the child's name). Dependency order stays
        children-before-parents at every level -- no per-level machinery.
        """
        reconstructor = contract.reconstructor
        # Prefer the contract's own parent naming when already rebound
        # (nested compound residuals); otherwise derive from the goal.
        if (getattr(contract, "provenance", None)
                and contract.provenance.get("rebound_parent_name")):
            parent_name = contract.provenance["rebound_parent_name"]
        else:
            # Nested contracts rebound by the decomposer use child names
            # that already ARE the parent node names of their subs. Detect
            # by checking whether reconstructor child_refs share a common
            # stem matching an existing naming pattern; default: derive.
            parent_name = _decomposition_base_name(contract.parent_goal)
            # If every child_ref starts with some stem + "_c", and the
            # children's names were rebound to `{X}_c{i}` where X itself
            # looks like a child of an outer parent, use the common prefix
            # before the final `_c{i}` as parent_name -- but only when the
            # contract was produced by `_rebind_contract` (child names are
            # exactly `{parent}_c{i}`).
            refs = list(reconstructor.child_refs)
            if refs and all("_c" in r for r in refs):
                # Common prefix stripped of trailing _c{i}
                stems = [r.rsplit("_c", 1)[0] for r in refs]
                if len(set(stems)) == 1 and stems[0]:
                    parent_name = stems[0]
        for child in contract.children:
            proj = list(child.projected_examples)
            sub = getattr(child, "sub_contract", None)
            if sub is not None:
                # Nested compound residual: the sub-parent IS this child.
                self._build_behavioral_graph(
                    graph, sub, proj, allow_effects)
            else:
                reuse_prim = getattr(child, "reuse_primitive", None)
                constraints = {"behavioral_component": {
                    "parent": parent_name,
                    "index": child.index,
                    "family": reconstructor.family,
                }}
                if (getattr(child, "source", None) == "behavioral_reuse"
                        and reuse_prim):
                    br = {"primitive": reuse_prim}
                    arg_bind = getattr(child, "reuse_arg_bind", None)
                    if arg_bind:
                        br["arg_bind"] = dict(arg_bind)
                    constraints["behavioral_reuse"] = br
                if (getattr(child, "source", None) == "behavioral_param"
                        and reuse_prim):
                    constraints["behavioral_param"] = {
                        "name": reuse_prim,
                    }
                requirement = CapabilityRequirement(
                    name=child.name, description=child.description,
                    constraints=constraints)
                node = RequirementNode(requirement=requirement)
                if (getattr(child, "source", None) == "behavioral_reuse"
                        and reuse_prim
                        and self.reg.get(reuse_prim) is not None):
                    # Pre-satisfied by behavioral match against a live
                    # acquired/promoted capability -- not a gap.
                    node.satisfied_by = SatisfiedBy.ACQUIRED
                    node.discovered_primitive = reuse_prim
                    node.evidence = (
                        f"behavioral reuse of existing capability "
                        f"{reuse_prim!r} (exact value-vector match)")
                    node.difficulty = 0.0
                elif (getattr(child, "source", None) == "behavioral_param"
                        and reuse_prim
                        and self.reg.get("identity") is not None):
                    # Pre-satisfied: param-exact component; assembly will
                    # bind identity(value=$param:<col>).
                    node.satisfied_by = SatisfiedBy.PRIMITIVE
                    node.discovered_primitive = "identity"
                    node.evidence = (
                        f"behavioral param-exact column {reuse_prim!r} "
                        f"(identity binding)")
                    node.difficulty = 0.0
                elif getattr(child, "source", None) == "behavioral_const":
                    # Pre-satisfied: constant component inlined at assembly.
                    node.satisfied_by = SatisfiedBy.PRIMITIVE
                    node.discovered_primitive = "__const__"
                    node.evidence = (
                        f"behavioral const {getattr(child, 'const_value', None)!r} "
                        f"(inline literal)")
                    node.difficulty = 0.0
                elif getattr(child, "source", None) == "behavioral_kv":
                    # Pre-satisfied: singleton dict wrap via kv at assembly.
                    node.satisfied_by = SatisfiedBy.PRIMITIVE
                    node.discovered_primitive = "__kv__"
                    node.evidence = (
                        f"behavioral kv wrap key="
                        f"{(getattr(child, 'reuse_arg_bind', None) or {}).get('__kv_key__')!r} "
                        f"value via {reuse_prim!r}")
                    node.difficulty = 0.0
                else:
                    self._resolve(node, child.description, allow_effects,
                                  examples=proj)
                graph.nodes[child.name] = node
                graph.node_examples[child.name] = proj
        parent_requirement = CapabilityRequirement(
            name=parent_name, description=contract.parent_goal,
            constraints={"behavioral_decomposition":
                         reconstructor.assembly_dict()})
        parent = RequirementNode(
            requirement=parent_requirement,
            depends_on=list(reconstructor.child_refs))
        self._resolve(parent, contract.parent_goal, allow_effects,
                      examples=list(goal_examples))
        graph.nodes[parent_name] = parent
        graph.node_examples[parent_name] = list(goal_examples)
        # Gap-A parity: record which registered primitives the discovered
        # children execute as, inside the parent's own behavioral metadata.
        # Param-exact children also record the parent column to bind.
        resolved = {}
        param_bindings = {}
        reuse_arg_bindings = {}
        const_bindings = {}
        kv_bindings = {}
        for child in contract.children:
            node = graph.nodes.get(child.name)
            if node is None:
                continue
            prim = node.discovered_primitive
            if prim:
                resolved[child.name] = prim
            if (getattr(child, "source", None) == "behavioral_param"
                    and getattr(child, "reuse_primitive", None)):
                param_bindings[child.name] = child.reuse_primitive
            arg_bind = getattr(child, "reuse_arg_bind", None)
            if (getattr(child, "source", None) == "behavioral_reuse"
                    and arg_bind):
                reuse_arg_bindings[child.name] = dict(arg_bind)
            if getattr(child, "source", None) == "behavioral_const":
                const_bindings[child.name] = getattr(child, "const_value", None)
            if getattr(child, "source", None) == "behavioral_kv":
                kv_bindings[child.name] = {
                    "value_ref": getattr(child, "reuse_primitive", None),
                    "bind": dict(getattr(child, "reuse_arg_bind", None) or {}),
                }
        bdecomp = parent.requirement.constraints["behavioral_decomposition"]
        if resolved:
            bdecomp["resolved_primitives"] = resolved
        if param_bindings:
            bdecomp["param_bindings"] = param_bindings
        if reuse_arg_bindings:
            bdecomp["reuse_arg_bindings"] = reuse_arg_bindings
        if const_bindings:
            bdecomp["const_bindings"] = const_bindings
        if kv_bindings:
            bdecomp["kv_bindings"] = kv_bindings
        return graph

    def _match_compound_shape(self, goal: str):
        lowered = goal.lower()
        for pattern, shape in _COMPOUND_SHAPES:
            if re.search(pattern, lowered):
                return shape
        return None

    def _single_requirement(self, goal: str) -> CapabilityRequirement:
        """Reuse the flat detector's keyword catalogue for the non-compound
        case, so the two paths agree on vocabulary rather than diverging."""
        from swarm_engine.acquisition.pipeline import GapDetector
        detector = GapDetector(self.reg, self.planner, self.composer)
        inferred = detector._infer_requirements(goal)
        return inferred[0] if inferred else CapabilityRequirement(
            name=re.sub(r"[^a-z0-9]+", "_", goal.lower()).strip("_")[:40] or "goal",
            description=goal)

    def _resolve(self, node: RequirementNode, description: str,
                 allow_effects: bool,
                 examples: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None
                 ) -> None:
        """Check the places a satisfying capability could already live."""
        name = node.name
        examples = list(examples) if examples else None

        # 0. an already-admitted capability may satisfy this description
        #    directly, independent of what auto-derived name this node
        #    happens to have. Found necessary directly: a capability
        #    admitted and goal-bound by AcquisitionOrchestrator was
        #    invisible to this method entirely, because check #1 below
        #    only ever looked in the primitive registry under node.name
        #    (frequently the generic literal "unclassified" for a
        #    top-level goal, never the capability's own registered name)
        #    and never consulted CapabilityStore's own goal/semantic
        #    bindings at all — the exact mechanism that already exists
        #    for this purpose. Generic: find_compatible() does real
        #    exact-then-behavioral matching: an exact goal binding is only
        #    honored when the admitted plan still reproduces the supplied
        #    examples (no verification bypass); otherwise, and for every
        #    non-exact goal, candidates are ranked by executing them on the
        #    examples with a similarity threshold. It is not a lookup table
        #    for any specific capability, and it returns nothing for a
        #    genuinely different or genuinely insufficient capability, so
        #    this cannot manufacture false satisfaction merely because
        #    *something* is registered.
        #
        #    The caller's worked examples are threaded through, because
        #    find_compatible() ranks behaviorally on examples — without
        #    them a reworded goal for already-acquired behavior is
        #    invisible at gap-analysis time even though the evidence to
        #    recognise it is in hand. Generic plumbing: the examples are
        #    the requirement's own, never synthesized here.
        if self.capabilities is not None:
            try:
                matches = self.capabilities.find_compatible(
                    description, examples=examples,
                    composer=self.composer, registry=self.reg)
            except Exception:
                matches = []
            if matches:
                rec = matches[0]
                # Satisfaction is exact, ranking is not: find_compatible()
                # is a RANKING function whose partial behavioral scores
                # receive a base bonus (0.4 + 0.6*b_score) that can clear
                # the similarity threshold on coincidental subset matches
                # (e.g. 1/4 examples agreeing at a single point). A
                # capability that fails ANY of the requirement's own
                # examples does NOT satisfy the requirement. When the
                # requirement supplies worked examples and a composer is
                # available, demand exact behavioral reproduction before
                # declaring the gap already closed; otherwise fall through
                # to the remaining checks below (a genuine gap must then
                # be acquired, not papered over by a near-miss).
                _b_exact = True
                if examples and self.composer is not None:
                    try:
                        from swarm_engine.synthesis.capability_match import (
                            behavioral_score as _bs)
                        _b_score, _ = _bs(rec, list(examples), self.composer)
                        _b_exact = _b_score >= 1.0
                    except Exception:
                        _b_exact = False
                if not _b_exact:
                    matches = []
            if matches:
                rec = matches[0]
                node.satisfied_by = SatisfiedBy.ACQUIRED
                via = getattr(rec, "via", "") or ""
                if via == "acquired_code":
                    # The executable identity is the entry's registered
                    # primitive name -- NOT this node's name. Recorded
                    # here (from the discovery result, not from goal
                    # text) so a decomposition parent can re-assemble
                    # over the discovered child via structural metadata.
                    node.discovered_primitive = rec.name
                    how = (f"behavioral match on the requirement's examples "
                           f"via acquired_code entry {rec.name!r}")
                else:
                    # Plan-native capability. Admitted capabilities are
                    # additionally registered as primitives under
                    # acquired.<capability_id> by admission, so the parent
                    # can re-assemble over it; pair-assembly's
                    # availability check fail-closes if it is absent.
                    node.discovered_primitive = (
                        f"acquired.{rec.capability_id}")
                    how = (f"match via capability store "
                           f"{rec.capability_id!r} ({rec.name!r})")
                node.evidence = (f"already-admitted capability "
                                f"{rec.capability_id!r} ({rec.name!r}) "
                                f"satisfies this requirement ({how})")
                node.difficulty = 0.0
                return

        # 1. already a primitive (built-in or previously acquired and
        #    registered — both live in the same registry once acquired).
        #    A node's own prerequisites can never satisfy it: without this
        #    exclusion a decomposition parent's name — a prefix of its
        #    children's names by construction — substring-matches its own
        #    children and would be marked satisfied before it is ever
        #    acquired. That is graph semantics (a dependent is not its
        #    prerequisites), not a naming special case, so it applies to
        #    every node.
        #    A decomposition parent additionally skips the fuzzy search
        #    entirely: its only valid satisfactions are a previously
        #    admitted capability (check #0 above), its exact registered
        #    name, or the validated pair-assembly run by the compose
        #    strategy — never a name-similar stranger.
        prereqs = set(node.depends_on or [])
        if name in self.reg:
            node.satisfied_by = (SatisfiedBy.ACQUIRED if name in self.acquired_specs
                                 else SatisfiedBy.PRIMITIVE)
            node.evidence = f"{name!r} is already registered"
            node.difficulty = 0.0
            return
        if not _is_decomposition_parent(node) and not _is_behavioral_parent(node):
            def _same_identity(prim_name, req_name):
                a = prim_name.lower().replace(" ", "_")
                b = req_name.lower().replace(" ", "_")
                return a == b
            hits = [p for p in self.reg.search(name)
                    if p.name not in prereqs
                    and _same_identity(p.name, name)]
            if hits:
                node.satisfied_by = (SatisfiedBy.ACQUIRED if name in self.acquired_specs
                                     else SatisfiedBy.PRIMITIVE)
                node.evidence = f"{name!r} is already registered"
                node.difficulty = 0.0
                return

        # 2. reachable by composing existing primitives for this description.
        # A template match alone is not enough: TemplatePlanner still returns
        # its best-scoring template even when nothing scores well, so a
        # nonsense goal can match "aggregate" on no real overlap and the
        # composer will happily analyze the resulting plan as type-correct.
        # That is precisely the impostor failure fixed elsewhere in synthesis
        # and recovery (see engine.py's SYNTHESIZE branch and
        # RecoveryEngine) — this is the same class of bug, just not yet
        # patched at this call site. The semantic matcher is what tells a
        # deliberate match from a coincidental one.
        #
        # Decomposition parents are exempt from this check: their only
        # legitimate composition is the pair-assembly of their own
        # children, behaviorally validated on the parent's examples by the
        # compose strategy. A planner-proposed template has not been
        # validated — and a single child alone can score "sufficient" on
        # word overlap while silently dropping the other output parts, a
        # concrete false-satisfaction this exemption closes.
        proposals = []
        if not _is_decomposition_parent(node) and not _is_behavioral_parent(node):
            proposals = self.planner.propose(description,
                                             allow_effects=allow_effects,
                                             limit=3)
        for proposal in proposals:
            if not proposal.strategy.startswith("template"):
                continue
            if not self.composer.analyze(proposal.plan).ok:
                continue
            match = self._matcher.score(
                description, description=" ".join(proposal.ops_used),
                ops_used=proposal.ops_used, strategy=proposal.strategy)
            if not match.sufficient:
                continue
            node.satisfied_by = SatisfiedBy.COMPOSITION
            node.evidence = f"composable from {proposal.ops_used} (match score {match.score:.2f})"
            node.difficulty = 0.1
            return

        # 3. a keyword hit elsewhere in the registry suggests an adjacent
        #    capability exists but not an exact match — real gap, but an
        #    easier one, since something related is already present.
        words = [w for w in re.split(r"[^a-z0-9]+", description.lower())
                if len(w) > 3]
        adjacent = any(self.reg.search(w) for w in words)
        node.satisfied_by = SatisfiedBy.NONE
        node.evidence = ("no matching or composable capability found"
                         + (" (related primitives exist)" if adjacent else ""))
        node.difficulty = 0.3 if adjacent else 0.7
