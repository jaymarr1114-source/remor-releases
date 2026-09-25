"""
swarm_engine/acquisition/orchestrator.py

Coordinated, recursive acquisition.

The gap reasoner says what is missing and in what order. This module actually
gets each piece, trying strategies for each gap in a fixed cost/risk order —
composition first (nothing new is trusted), then generation (SWarm builds it),
then experimentation (induced from examples, weakest and labelled as such),
then delegation and retrieval if available — and it acquires dependencies
before the things that depend on them, so "acquire B, then use B to build the
composite for X" is a real sequence rather than one flat attempt.

Every strategy funnels through the same independent validator and the same
registrar. No strategy gets to skip verification for having been chosen
"first": the ordering is about which is *tried* first, not which is trusted
more once it produces something.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from swarm_engine.acquisition.gap_reasoner import (
    CapabilityGapReasoner, RequirementGraph, RequirementNode, SatisfiedBy,
    _is_behavioral_parent, _is_decomposition_parent,
)
from swarm_engine.acquisition.decomposition import BehavioralDecomposer
from swarm_engine.acquisition.semantic import Case
from swarm_engine.acquisition.strategies import (
    CapabilitySpec, DelegatingSource, ExampleInducer, Strategy,
    StrategySelector, SynthesizingSource,
)
from swarm_engine.synthesis.acquisition_learning import AcquisitionLearner
from swarm_engine.synthesis.capability_store import stable_code_id
from swarm_engine.verification.independent import IndependentValidator


# ---------------------------------------------------------------------------
# Requirement signature + experience-driven strategy ordering (Boundary 2).
#
# The signature is deliberately goal-agnostic: it is built ONLY from
# structural features of the requirement spec that are available before any
# strategy is tried (worked-example presence/count, input arity, whether the
# selector deemed the requirement composable, a coarse output-type class,
# required effects, and the acquisition target's generic class). It never
# contains goal text, goal keywords, or per-objective branches, so learned
# preferences transfer across goals that share structure, not vocabulary.
# ---------------------------------------------------------------------------

_OUTPUT_CATEGORIES = (
    ("bool", "bool"), ("int", "int"), ("float", "float"),
    ("str", "str"), ("list", "list"), ("dict", "dict"), ("tuple", "tuple"),
)

_TARGET_CLASSES = {
    "structural_representation_capability": "structural",
    "semantic_operation_interpretation": "semantic",
}


def _output_category(output_kind: object) -> str:
    kind = str(output_kind or "").lower()
    if not kind:
        return "none"
    for token, label in _OUTPUT_CATEGORIES:
        if token in kind:
            return label
    return "other"


def requirement_signature(spec: "CapabilitySpec",
                          choices: Sequence["StrategyChoice"]) -> str:
    """Goal-agnostic signature of a requirement, used as the learner key.

    Derived only from requirement features, never from the goal's text:
    two differently-worded goals with the same structure share a signature,
    and no goal-specific word can ever appear in it.
    """
    target = getattr(spec, "acquisition_target", None) or {}
    tgt = _TARGET_CLASSES.get(str(target.get("class") or ""), "none")
    n_examples = len(getattr(spec, "examples", None) or [])
    ex_band = "0" if n_examples == 0 else ("1-2" if n_examples <= 2 else "3+")
    n_inputs = len(getattr(spec, "input_names", None) or [])
    in_band = ("0" if n_inputs == 0 else "1" if n_inputs == 1
               else "2" if n_inputs == 2 else "3+")
    composable = "y" if any(
        c.strategy == Strategy.COMPOSE for c in choices) else "n"
    out = _output_category(getattr(spec, "output_kind", ""))
    effects = "y" if getattr(spec, "required_effects", None) else "n"
    return (f"tgt:{tgt}|ex:{ex_band}|in:{in_band}|"
            f"comp:{composable}|out:{out}|eff:{effects}")


def reorder_by_experience(learner: AcquisitionLearner,
                          choices: List["StrategyChoice"],
                          signature: str,
                          policy: Optional[str] = None) -> List["StrategyChoice"]:
    """Reorder strategy choices using the learner's stored experience.

    The ONLY evidence consulted is the learner's stored per-(signature,
    strategy) stats, via AcquisitionLearner.prefer(). With no experience
    for the signature, prefer() is a stable sort over all-untried
    candidates and the input (fixed) order is returned unchanged. No
    strategy name is ever special-cased here.

    2026-09-19: `policy` scopes the evidence to the current search-policy
    version -- strategy outcomes recorded under an older policy are stale
    (the policy that produced them has been repaired) and must not steer
    ordering. Mirrors the 2026-09-14 failure_memory scoping.
    """
    names = [c.strategy.value for c in choices]
    ordered = learner.prefer(names, signature, policy=policy)
    by_name = {c.strategy.value: c for c in choices}
    return [by_name[n] for n in ordered if n in by_name]


def apply_acquisition_pruning(choices: List["StrategyChoice"], signature: str,
                              pruning: Optional[Dict[str, Any]]) -> List["StrategyChoice"]:
    """Drop strategies an ACTIVE acquisition-policy improvement pruned for
    this signature (Boundary 4).

    `pruning` is {signature: {strategy_name, ...}} as maintained on the
    engine by ImprovementPipeline._activate for the
    "acquisition.strategy_policy" subsystem, or None when no improvement
    is active. None/empty pruning is a pure no-op.

    Pruning only ever REMOVES candidates from the trial list. Everything
    that remains goes through the exact same handlers, the same
    IndependentValidator/admission path, and the same registrar as
    before — a pruned strategy cannot weaken verification because
    verification never depended on which strategies were tried.
    """
    if not pruning:
        return choices
    def _family(sig):
        return "|".join(p for p in str(sig).split("|") if not p.startswith("comp:"))
    pruned = set(pruning.get(signature) or [])
    fam = _family(signature)
    for k, v in pruning.items():
        if _family(k) == fam:
            pruned.update(v)
    if not pruned:
        return choices
    return [c for c in choices if c.strategy.value not in pruned]



def _general_skip_suppresses(engine, learner, signature, strategy_name, policy):
    if not signature or learner is None:
        return None
    general_skip = getattr(engine, "acquisition_general_skip", None) or {}
    rule = general_skip.get(strategy_name)
    if not rule:
        return None
    if signature in (rule.get("except_signatures") or []):
        return None
    try:
        max_failures = int(rule.get("max_failures", 3))
    except (TypeError, ValueError):
        return None
    stats = learner.stats(signature, strategy_name, policy=policy)
    attempts = int(stats.get("attempts", 0) or 0)
    successes = int(stats.get("successes", 0) or 0)
    if attempts >= max_failures and successes == 0:
        return {"failures": attempts, "max_failures": max_failures}
    return None


@dataclass
class AttemptRecord:
    node: str
    strategy: str
    accepted: bool
    detail: str = ""
    capability_id: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"node": self.node, "strategy": self.strategy,
                "accepted": self.accepted, "detail": self.detail[:200],
                "capability_id": self.capability_id}


@dataclass
class OrchestrationResult:
    goal: str
    graph: Dict[str, Any] = field(default_factory=dict)
    attempts: List[AttemptRecord] = field(default_factory=list)
    acquired: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    # Diagnostic-only records.  These do not make a requirement resolved or
    # imply that a semantic hypothesis, source, or implementation exists.
    semantic_evidence_gaps: List[Dict[str, Any]] = field(default_factory=list)
    # Names/ids removed by transactional rollback when the graph did not
    # fully resolve (e.g. "primitive:foo", "capability:acq_..."). Empty on
    # success. Historical evidence (learner rows, failure memory) is kept.
    rolled_back: List[str] = field(default_factory=list)

    @property
    def fully_resolved(self) -> bool:
        return not self.failed

    def as_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "graph": self.graph,
                "attempts": [a.as_dict() for a in self.attempts],
                "acquired": self.acquired, "failed": self.failed,
                "rolled_back": self.rolled_back,
                "semantic_evidence_gaps": self.semantic_evidence_gaps,
                "fully_resolved": self.fully_resolved}


class AcquisitionOrchestrator:
    """Resolves a goal's full requirement graph, not just one gap.

    A single missing capability is the easy case the old pipeline already
    handled. This exists for the harder one: a goal needing several missing
    pieces in a dependency order, where an earlier acquisition changes what is
    composable for a later one — B is acquired, then the composite for "X"
    becomes reachable because B is now a primitive in the registry.
    """

    def __init__(self, engine, learn_from_experience: bool = True):
        self.engine = engine
        self.learn_from_experience = learn_from_experience
        self.reasoner = CapabilityGapReasoner(
            engine.primitives, engine.planner, engine.composer,
            engine.provenance, engine.acquired_specs,
            capabilities=engine.capabilities,
            decomposer=BehavioralDecomposer(engine.primitives))
        self.selector = StrategySelector()
        # O15: the synthesizer's registry handles are threaded so a
        # caller-supplied spec_provider is bound when one is installed.
        self.synthesizer = SynthesizingSource(
            oracle_registry=getattr(engine, "oracle_registry", None),
            engine_oracle=getattr(engine, "oracle", None))
        self.inducer = ExampleInducer()
        self._attempt_listener = None
        # O16: driver credentials for delegate identity binding, set per
        # resolve() call (the delegate's producer is the driver).
        self._delegate_producer_id = None
        self._delegate_token = None

    @staticmethod
    def _strategy_failure_context() -> str:
        """The search-policy context for strategy-failure records.

        2026-09-14: a strategy failure is evidence about the search policy
        that produced it. When the policy is repaired, earlier failures are
        stale and must not keep blocking retries — so failure records are
        tagged with the policy version and the memory-informed skip only
        counts failures recorded under the CURRENT policy. Imported lazily
        to keep module import order acyclic.
        """
        from swarm_engine.cognition.synthesis import SEARCH_POLICY_VERSION
        return f"search-policy-v{SEARCH_POLICY_VERSION}"

    def _active_learner(self) -> Optional[AcquisitionLearner]:
        """The experience store driving strategy order, or None to bypass.

        The learner is owned by the engine (persisted in the engine's own
        DB) so experience survives across orchestrator instances and across
        fresh engine objects on the same DB file. Bypassed (returning None)
        when learning is disabled on this orchestrator or no learner is
        attached -- in which case the selector's fixed order is used and
        nothing is recorded.
        """
        if not self.learn_from_experience:
            return None
        learner = getattr(self.engine, "strategy_learner", None)
        if learner is None:
            # Engines constructed before the learner existed still get one,
            # attached to the engine on its own DB path so the experience
            # persists in engine storage rather than in orchestrator memory.
            learner = AcquisitionLearner(
                getattr(self.engine, "db_path", "swarm_engine.db"))
            self.engine.strategy_learner = learner
        return learner


    def _ordered_acquisition_nodes(self, graph) -> list:
        """Topological order with learned preference among currently ready nodes.

        Uses a Kahn-style ready set so preferences apply at each decision
        frontier, not only within static depth buckets. Dependency edges
        are always respected: a node enters the ready set only when all of
        its gap-prerequisites are already scheduled.
        """
        gaps = {n.name: n for n in graph.gaps()}
        if not gaps:
            return []
        # remaining prerequisite count within the gap subgraph
        remaining = {}
        dependents = {name: [] for name in gaps}
        for name, node in gaps.items():
            deps = [d for d in (node.depends_on or []) if d in gaps]
            remaining[name] = len(deps)
            for d in deps:
                dependents[d].append(name)
        ready = [n for n, c in remaining.items() if c == 0]
        learner = self._active_learner()
        ordered_names: list = []
        while ready:
            if learner is not None and len(ready) > 1:
                try:
                    ranked = learner.prefer(list(ready), "node_order")
                    # prefer may omit unknowns; keep only ready members
                    pick_order = [n for n in ranked if n in ready]
                    for n in ready:
                        if n not in pick_order:
                            pick_order.append(n)
                    ready = pick_order
                except Exception:
                    ready = sorted(ready)  # stable fallback
            else:
                # stable: preserve insertion / name order when no signal
                ready = list(ready)
            pick = ready.pop(0)
            ordered_names.append(pick)
            for dep in dependents.get(pick, []):
                remaining[dep] -= 1
                if remaining[dep] == 0:
                    ready.append(dep)
        # append any nodes not reached (cycle / orphan) in original gap order
        if len(ordered_names) < len(gaps):
            for n in gaps:
                if n not in ordered_names:
                    ordered_names.append(n)
        by = {n.name: n for n in graph.gaps()}
        return [by[n] for n in ordered_names if n in by]

    async def resolve(self, goal: str,
                      examples_by_node: Optional[Dict[str, Sequence[Tuple[Dict[str, Any], Any]]]] = None,
                      delegate: Optional[Callable] = None,
                      allow_effects: bool = False,
                      examples: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None,
                      examples_batch_id: Optional[str] = None,
                      delegate_producer_id: Optional[str] = None,
                      delegate_token: Optional[str] = None,
                      ) -> OrchestrationResult:
        """Resolve a goal's full requirement graph.

        `examples` are the goal's OWN worked examples. When the goal's
        outputs are uniformly composite, the gap reasoner structurally
        decomposes the requirement into per-element sub-requirements (each
        projected from these examples) plus a parent re-assembled from the
        acquired parts -- recursive acquisition rather than one flat attempt.

        `examples_batch_id` (O19) is the content-addressed provenance id of
        the driver example set; when omitted but `examples` are present it
        is recorded here (idempotent).

        `delegate_producer_id` / `delegate_token` (O16) authenticate the
        driver as the delegate's producer in bound mode; without them an
        unattributed delegate is refused.
        """
        self._delegate_producer_id = delegate_producer_id
        self._delegate_token = delegate_token
        # O19: provenance for the goal's own worked examples. engine.resolve
        # records the same batch upstream; this covers direct callers.
        if examples_batch_id is None and examples:
            from swarm_engine.governance.examples_provenance import (
                record_examples_batch)
            _reg = getattr(self.engine, "oracle_registry", None)
            _handle = getattr(self.engine, "oracle", None)
            if _reg is not None and _handle is not None:
                examples_batch_id = record_examples_batch(
                    _reg, _handle, goal, list(examples),
                    "acquisition_orchestrator.resolve")
        graph = self.reasoner.analyze(
            goal, allow_effects=allow_effects,
            examples_by_node=examples_by_node, examples=examples)
        result = OrchestrationResult(goal=goal, graph=graph.as_dict())
        examples_by_node = examples_by_node or {}
        # Transactional acquisition: snapshot everything this run could add
        # (registered primitives, persisted acquired source, stored
        # capabilities and their goal bindings). If the graph does not fully
        # resolve, the run is rolled back to the snapshot — a failed graph
        # must not leave partially acquired prerequisites behind, because a
        # part acquired for a composition that never validated is not a
        # capability the engine can honestly claim to have. Historical
        # evidence (learner rows, failure memory, provenance, lifecycle) is
        # deliberately NOT rolled back: the attempt happened and the system
        # should remember it.
        snapshot = self._acquisition_snapshot()

        # The goal's own worked examples, for nodes that carry no more
        # specific ones (a flat, non-decomposed graph has no per-node
        # projections; the goal's examples ARE the node's examples).
        goal_examples = list(examples or [])
        for node in self._ordered_acquisition_nodes(graph):
            try:
                self.engine.budget.spend(acquisitions=1)
            except Exception as exc:
                result.failed.append(node.name)
                result.attempts.append(AttemptRecord(node.name, "budget", False, str(exc)))
                break

            # The reasoner's own per-node examples (projected parts for a
            # decomposed goal) take precedence; caller-supplied per-node
            # examples are next; the goal's own examples are the final
            # fallback. Found directly: without the last fallback a flat
            # goal's examples never reached the strategy specs, so
            # generation refused for "no examples" on a goal that had
            # eight.
            node_examples = (list(graph.node_examples.get(node.name) or [])
                             or list(examples_by_node.get(node.name, []))
                             or goal_examples)
            # O19: only the goal's OWN examples carry the goal batch id --
            # per-node projections are derived sets, not the recorded batch.
            if examples_batch_id is not None and node_examples is goal_examples:
                node.requirement.examples_batch_id = examples_batch_id
            spec = CapabilitySpec.from_requirement(node.requirement,
                                                   node_examples)

            # A missing dependency this node relies on means there is nothing
            # to compose or generalise from yet. Recorded, not silently
            # skipped, because "blocked on a prerequisite" is a different
            # outcome from "tried and failed" and the caller needs to see it.
            unmet = [d for d in node.depends_on if d in result.failed]
            if unmet:
                result.failed.append(node.name)
                result.attempts.append(AttemptRecord(
                    node.name, "blocked", False,
                    f"depends on {unmet} which could not be acquired"))
                continue

            success = await self._resolve_node(node, spec, delegate, result)
            (result.acquired if success else result.failed).append(node.name)
            # Feed recursive node-order learning
            try:
                learner = self._active_learner()
                if learner is not None:
                    cost = 1
                    for a in reversed(result.attempts):
                        if getattr(a, "node", None) == node.name:
                            break
                    learner.record("node_order", node.name, bool(success), cost)
            except Exception:
                pass
            if success:
                # Gap-A extension: a node acquired during this run must
                # become visible to decomposition parents assembled later
                # in the same run (see _record_acquired_primitive).
                self._record_acquired_primitive(node, result, graph)

        if not result.fully_resolved:
            result.rolled_back = self._rollback_to_snapshot(snapshot)
        return result

    def _record_acquired_primitive(self, node, result, graph) -> None:
        """Make a run-acquired node addressable to later-assembled parents.

        Gap-A (`resolved_primitives`, built by the gap reasoner at analysis
        time) maps child node names to the registered primitive names of
        children discovered BEFORE the run. It cannot know the names of
        children acquired DURING the run: the primary GENERATE path
        registers under the node name, but every admission path (the
        symbolic-search fallback, compose, pair-assembly) registers under
        the admission-assigned primitive name (`acquired.<capability_id>`).
        A decomposition parent assembled later in the same run then
        honestly refuses -- its children are acquired but invisible --
        and the whole graph rolls back.

        This generalizes Gap-A to acquisition time: after a node succeeds,
        determine the primitive name it actually executes under from live
        registry state (never goal text, never fixture identity) and record
        node.name -> primitive name into each dependent parent's
        `output_decomposition.resolved_primitives`. The parent's spec is
        built from its requirement AFTER its dependencies resolve, so both
        the selector's composability check and `_try_pair_assembly` see the
        update through the existing map with no signature changes.

        Run-scoped by construction: the graph is rebuilt on every
        `analyze()`, and a failed run's rollback removes the registered
        primitives anyway.
        """
        prim_name: Optional[str] = None
        if self.engine.primitives.get(node.name) is not None:
            # Primary GENERATE / DELEGATE path: registered under the node
            # name itself.
            prim_name = node.name
        else:
            # Admission paths: find the primitive the admitted capability
            # was registered as, via the accepted attempt's capability id.
            cap_id = ""
            for attempt in reversed(result.attempts):
                if (attempt.node == node.name and attempt.accepted
                        and attempt.capability_id):
                    cap_id = attempt.capability_id
                    break
            if cap_id:
                # Prefer the registry's own capability->primitive index
                # over the naming convention.
                acq_ids = (getattr(self.engine.primitives,
                                   "_acquired_capability_ids", None)
                           or {})
                for registered_name, cid in acq_ids.items():
                    if cid == cap_id and self.engine.primitives.get(
                            registered_name) is not None:
                        prim_name = registered_name
                        break
                if prim_name is None and self.engine.primitives.get(
                        f"acquired.{cap_id}") is not None:
                    # Admission's documented primitive-naming convention
                    # (admission._register_capability_as_primitive).
                    prim_name = f"acquired.{cap_id}"
        if prim_name is None:
            prim_name = getattr(node, "discovered_primitive", None)
        if not prim_name:
            return
        # Expose assembled/admitted parents under their graph node name so
        # project binding and later composition can resolve node.name → callable.
        if prim_name != node.name and self.engine.primitives.get(node.name) is None:
            src = self.engine.primitives.get(prim_name)
            if src is not None:
                try:
                    from swarm_engine.primitives.core import Primitive
                    alias = Primitive(
                        name=node.name,
                        family=getattr(src, "family", "acquired"),
                        fn=src.fn,
                        inputs=dict(getattr(src, "inputs", {}) or {}),
                        output=getattr(src, "output", None),
                        effects=tuple(getattr(src, "effects", ()) or ()),
                        doc=f"alias of {prim_name} for node {node.name}",
                    )
                    self.engine.primitives.register(alias, overwrite=True)
                    # Track the alias in the tag index so revocation can
                    # find and unregister it: a quarantined capability must
                    # not remain callable under its node-name alias
                    # (2026-09-19 R11).
                    try:
                        ti = getattr(self.engine.primitives,
                                     "_acquired_capability_ids", None)
                        if isinstance(ti, dict):
                            ti[node.name] = prim_name.split(".", 1)[1] \
                                if prim_name.startswith("acquired.") \
                                else prim_name
                    except Exception:
                        pass
                except Exception:
                    pass
        for parent in graph.nodes.values():
            constraints = getattr(parent.requirement, "constraints", None) or {}
            # Structural and behavioral (P8) parents both keep a
            # child->primitive map their assembly consults at compose time.
            for _key in ("output_decomposition", "behavioral_decomposition"):
                assembly = constraints.get(_key)
                if not isinstance(assembly, dict):
                    continue
                child_keys = list(assembly.get("children")
                                  or assembly.get("child_refs") or [])
                # v41: hierarchical wrap child_refs often carry a
                # decomposer suffix (e.g. "..._w0") while the acquired
                # graph node uses the rebound stem without that suffix.
                # Match exact OR stem/prefix so resolved_primitives is
                # updated under the key assembly will actually look up.
                matched = []
                for ck in child_keys:
                    if ck == node.name:
                        matched.append(ck)
                    elif ck.startswith(node.name + "_") or node.name.startswith(ck + "_"):
                        matched.append(ck)
                if not matched:
                    continue
                resolved = assembly.get("resolved_primitives")
                if not isinstance(resolved, dict):
                    resolved = {}
                    assembly["resolved_primitives"] = resolved
                for ck in matched:
                    resolved[ck] = prim_name
                resolved[node.name] = prim_name

    # -- transactional acquisition ------------------------------------------
    def _acquisition_snapshot(self) -> Dict[str, Any]:
        """Capture every active-capability location a run could add to."""
        eng = self.engine
        return {
            "primitives": set(eng.primitives.names()),
            "acquired_code": {r["name"] for r in eng.acquired_code.all()},
            "acquired_specs": set(eng.acquired_specs),
            "capabilities": {r.capability_id
                             for r in eng.capabilities.list(limit=100000)},
            "goal_bindings": eng.capabilities.goal_bindings(),
        }

    def _rollback_to_snapshot(self, snapshot: Dict[str, Any]) -> List[str]:
        """Remove everything acquired since the snapshot; return what went.

        Only active capability state is removed: registered primitives that
        did not exist before the run, persisted acquired source, acquired
        spec records, newly stored capabilities, and goal bindings the run
        created or replaced (replaced bindings are restored, not just
        dropped). Pre-existing capabilities — including ones this run
        merely reused — are untouched.
        """
        eng = self.engine
        removed: List[str] = []

        for name in eng.primitives.names():
            if name not in snapshot["primitives"]:
                if eng.primitives.unregister(name):
                    removed.append(f"primitive:{name}")
        for rec in eng.acquired_code.all():
            if rec["name"] not in snapshot["acquired_code"]:
                eng.acquired_code.forget(rec["name"])
                removed.append(f"acquired_code:{rec['name']}")
        for name in list(eng.acquired_specs):
            if name not in snapshot["acquired_specs"]:
                del eng.acquired_specs[name]
                removed.append(f"acquired_spec:{name}")
        for rec in eng.capabilities.list(limit=100000):
            if rec.capability_id not in snapshot["capabilities"]:
                if eng.capabilities.delete_capability(rec.capability_id):
                    removed.append(f"capability:{rec.capability_id}")
        current_bindings = eng.capabilities.goal_bindings()
        for goal_key, cap_id in current_bindings.items():
            old = snapshot["goal_bindings"].get(goal_key)
            if old is None:
                if eng.capabilities.unbind_goal_if(goal_key, cap_id):
                    removed.append(f"goal_binding:{goal_key}")
            elif old != cap_id:
                eng.capabilities.bind_goal(goal_key, old)
                removed.append(f"goal_binding:{goal_key}->restored")
        return sorted(removed)

    async def _resolve_node(self, node: RequirementNode, spec: CapabilitySpec,
                            delegate: Optional[Callable],
                            result: OrchestrationResult) -> bool:
        signature, success = await self._resolve_node_impl(
            node, spec, delegate, result)
        self._record_policy_outcome(signature, success)
        return success

    def _record_policy_outcome(self, signature: Optional[str],
                               success: bool) -> None:
        """Feed one real post-activation outcome back to the improvement
        pipeline (Boundary 4): when an ACTIVE acquisition-policy
        improvement governed this node (its signature is pruned), the
        outcome is recorded so check_for_regression can evidence-drive a
        rollback. Monitoring must never break acquisition, so failures
        here are contained, not propagated."""
        if signature is None:
            return
        pruning = getattr(self.engine, "acquisition_pruning", None) or {}
        if signature not in pruning:
            return
        pipe = getattr(self.engine, "acquisition_improvement_pipeline", None)
        if pipe is None:
            return
        try:
            pipe.record_production_outcome("acquisition.strategy_policy",
                                           bool(success))
        except Exception:
            pass


    def _heldout_admission_gate(self, spec: CapabilitySpec, capability_id: str,
                                min_heldout: int = 1) -> Tuple[bool, str]:
        """Post-hoc admission consistency check plus synthetic novelty probe.

        Re-verifies the admitted capability on the last-third example split
        (a consistency re-check: synthesis saw these examples, so this alone
        cannot catch memorisation) and then requires non-null output on
        synthetic UNSEEN inputs (scalars beyond the seen range; shape-faithful
        perturbed lists). A pure lookup table returns null on unseen inputs
        and is rejected as non-generalizing. True held-out proof (inputs
        never shown to synthesis) is the caller's responsibility.
        When fewer than 2 examples exist, gate is skipped (insufficient split).
        """
        examples = list(spec.examples or [])
        if len(examples) < 2:
            return True, "heldout_skipped_insufficient_examples"
        # Hold out the last third (at least 1)
        n_hold = max(1, len(examples) // 3)
        heldout = examples[-n_hold:]
        prim = self.engine.primitives.get(capability_id)
        rec = self.engine.capabilities.get(capability_id)
        if prim is None:
            aliases = [capability_id, f"acquired.{capability_id}"]
            if rec is not None:
                aliases.extend([
                    getattr(rec, "name", "") or "",
                    f"acquired.{getattr(rec, 'capability_id', '')}",
                ])
            # acquire_capability often registers under the node/spec name
            # while held-out is keyed by capability_id — scan acquired_code
            if hasattr(self.engine, "acquired_code"):
                for row in self.engine.acquired_code.all():
                    if row.get("capability_id") == capability_id or row.get("id") == capability_id:
                        aliases.append(row.get("name") or "")
            # Also: any primitive whose registration notes mention the id
            try:
                for n in self.engine.primitives.names():
                    if capability_id in n or n in capability_id:
                        aliases.append(n)
            except Exception:
                pass
            seen = set()
            for name in aliases:
                if not name or name in seen:
                    continue
                seen.add(name)
                prim = self.engine.primitives.get(name)
                if prim is not None:
                    break
        fn = None
        if prim is not None:
            fn = prim.fn if hasattr(prim, "fn") else prim
        if fn is None and rec is not None:
            fn = getattr(rec, "fn", None) or getattr(rec, "callable", None)
        if fn is None:
            return False, "heldout_no_primitive"
        for args, expect in heldout:
            try:
                got = fn(**dict(args)) if isinstance(args, dict) else fn(args)
            except Exception as e:
                return False, f"heldout_error:{e}"
            if got != expect:
                return False, f"heldout_mismatch: got={got!r} expect={expect!r}"
        # 2026-09-19: synthetic unseen-input check. The split above can
        # still use inputs the capability memorized (a discrete_lookup
        # built from all examples passes it trivially). Generate inputs
        # not present in the example set; a generalizing capability must
        # produce non-null output, while a pure lookup returns null and
        # is rejected as non-generalizing.
        try:
            _seen_keys = set()
            _seen_lists = []
            for _a, _v in examples:
                _d = dict(_a) if isinstance(_a, dict) else {}
                if len(_d) == 1:
                    _val = tuple(_d.values())[0]
                    # 2026-09-22 (R5): never let one unhashable key abort the
                    # whole synthetic check — skip it individually so scalar
                    # inputs still get unseen-input coverage.
                    try:
                        _seen_keys.add(_val)
                    except TypeError:
                        pass
                    if isinstance(_val, list):
                        _seen_lists.append(_val)
            _synthetic = []
            _num_seen = [_k for _k in _seen_keys
                         if isinstance(_k, (int, float))
                         and not isinstance(_k, bool)]
            if _num_seen and len(_seen_keys) == len(_num_seen):
                _mx = max(_num_seen)
                _step = 1
                if all(float(_k).is_integer() for _k in _num_seen):
                    _step = 1
                for _i in (1, 2):
                    _cand = _mx + _step * _i
                    if _cand not in _seen_keys:
                        _synthetic.append(_cand)
            # 2026-09-22 (R5): parity for list inputs. A pure table over list
            # keys returns null on an unseen list just as on an unseen scalar.
            # The probe must be SHAPE-FAITHFUL (same element kinds, novel
            # values) or it would false-reject shape-sensitive capabilities.
            def _nov(v):
                if isinstance(v, bool):
                    return not v
                if isinstance(v, int):
                    return v + 1009
                if isinstance(v, float):
                    return v + 1009.5
                if isinstance(v, str):
                    return v + "_novel"
                return v
            for _lst in _seen_lists:
                try:
                    if _lst and all(isinstance(e, dict) for e in _lst):
                        _ks = list(_lst[0].keys())
                        if not _ks or not all(
                                set(e.keys()) == set(_ks) for e in _lst):
                            continue
                        _probe = list(_lst) + [
                            {k: _nov(_lst[0][k]) for k in _ks}]
                    elif _lst and all(isinstance(e, list) for e in _lst):
                        _n = max(len(e) for e in _lst)
                        _probe = list(_lst) + [
                            [_nov(7 + i) for i in range(_n)]]
                    else:
                        _leaves = [e for e in _lst
                                   if isinstance(e, (int, float, str))
                                   and not isinstance(e, bool)]
                        _probe = list(_lst) + [
                            _nov(_leaves[0]) if _leaves else 1009]
                except Exception:
                    continue
                if _probe not in _seen_lists:
                    _synthetic.append(_probe)
                    break
            _in_name = None
            if examples:
                _ed = dict(examples[0][0]) if isinstance(examples[0][0], dict) else {}
                if len(_ed) == 1:
                    _in_name = list(_ed.keys())[0]
            for _sk in _synthetic:
                try:
                    if _in_name:
                        _got = fn(**{_in_name: _sk})
                    else:
                        _got = fn(_sk)
                except Exception:
                    _got = None
                if _got is None:
                    return False, (f"heldout_nongeneralizing: null on "
                                   f"unseen input {_sk!r}")
        except Exception:
            pass
        # Persist evidence on capability if store supports it
        try:
            if hasattr(self.engine.capabilities, "log"):
                self.engine.capabilities.log(
                    capability_id, "heldout_admitted",
                    f"passed {len(heldout)} held-out cases")
        except Exception:
            pass
        return True, f"heldout_passed:{len(heldout)}"

    def _reject_capability(self, capability_id: str, reason: str) -> None:
        """Quarantine/reject a capability that failed held-out admission."""
        try:
            if hasattr(self.engine.capabilities, "set_status"):
                self.engine.capabilities.set_status(capability_id, "quarantined")
            if hasattr(self.engine.capabilities, "log"):
                self.engine.capabilities.log(capability_id, "heldout_rejected", reason)
            # Unbind from goal if bound
            if hasattr(self.engine.capabilities, "unbind_goal_if"):
                pass
        except Exception:
            pass

    async def _resolve_node_impl(self, node: RequirementNode, spec: CapabilitySpec,
                                 delegate: Optional[Callable],
                                 result: OrchestrationResult) -> Tuple[Optional[str], bool]:
        if not node.is_gap:
            result.attempts.append(AttemptRecord(
                node.name, "already_satisfied", True, node.evidence))
            return None, True

        choices = self.selector.select(
            spec, registry=self.engine.primitives,
            network_available=False,  # no network in this environment; honest
            delegate_available=delegate is not None)

        # Boundary 2: reorder the selector's fixed candidate order using
        # learned experience for this requirement's signature BEFORE any
        # strategy is tried. With no experience the learner's prefer() is a
        # stable no-op, so the fixed order is used unchanged. Every attempt
        # outcome is then recorded back with a real measured cost, closing
        # the loop. Ordering only changes which strategy is TRIED first --
        # admission/verification of whatever a strategy produces is
        # untouched, so no strategy can be trusted more for being early.
        learner = self._active_learner()
        # The signature is computed unconditionally now (it is a pure
        # function of the requirement): the acquisition-policy pruning
        # (Boundary 4) is keyed on it and applies whether or not the
        # experience learner is enabled.
        signature: str = requirement_signature(spec, choices)
        if learner is not None:
            choices = reorder_by_experience(
                learner, choices, signature,
                policy=self._strategy_failure_context())
        # 2026-09-19 (R10): EXPERIMENT is the non-generalizing
        # discrete_lookup fallback -- it admits a finite table that the
        # IndependentValidator rejects as non-generalising by design. A
        # degenerate admission must never preempt a generalizing strategy:
        # if the learner ranks EXPERIMENT first (because a lookup
        # "succeeded" for a same-signature goal whose true law the
        # generator could not reach), every later goal with that signature
        # gets a table instead of a law, even when GENERATE would have
        # found one (observed directly: x^2+3's lookup success steered x^4
        # to experiment-first, and x^4 -- generable on a fresh DB -- was
        # admitted as a lookup without GENERATE ever being tried). The
        # learner still orders the generalizing strategies among
        # themselves; EXPERIMENT is stable-partitioned to the end so it
        # remains what it is: the last resort. For genuinely tabular
        # goals the generalizing strategies fail and EXPERIMENT still
        # fires -- only later, never instead.
        _exp = [c for c in choices if c.strategy == Strategy.EXPERIMENT]
        if _exp:
            _rest = [c for c in choices if c.strategy != Strategy.EXPERIMENT]
            choices = _rest + _exp
        # Boundary 4: drop strategies an active improvement pruned for this
        # signature. No-op when no improvement is active.
        choices = apply_acquisition_pruning(
            choices, signature,
            getattr(self.engine, "acquisition_pruning", None))

        # Decomposition parents (structural or behavioral/P8) whose
        # children are all available: COMPOSE is not a heuristic here but
        # the structurally correct action -- the graph was built precisely
        # to assemble these children. The experience learner's reordering
        # (e.g. GENERATE succeeding on the child leaves, which share the
        # goal-agnostic signature) must not promote a synthesizing strategy
        # ahead of the assembly. This enforces the selector's
        # "composable ⇒ COMPOSE first" intent. Not per-goal: keyed on the
        # parent metadata the reasoner attached, never on goal text.
        if _is_decomposition_parent(node) or _is_behavioral_parent(node):
            _cf = [c for c in choices if c.strategy == Strategy.COMPOSE]
            if _cf:
                choices = _cf + [c for c in choices
                                 if c.strategy != Strategy.COMPOSE]

        def _record_attempt(strategy_name: str, accepted: bool,
                            cost_ns: int) -> None:
            if learner is not None and signature is not None:
                learner.record(signature, strategy_name,
                               success=accepted, cost=cost_ns,
                               policy=self._strategy_failure_context())
            listener = getattr(self, "_attempt_listener", None)
            if listener is not None and signature is not None:
                try:
                    listener(node.name, signature, strategy_name, 0,
                             accepted, cost_ns, False)
                except Exception:
                    pass

        for choice in choices:
            _skip = _general_skip_suppresses(
                self.engine, learner, signature, choice.strategy.value,
                self._strategy_failure_context())
            if _skip is not None:
                result.attempts.append(AttemptRecord(
                    node.name, choice.strategy.value, False,
                    f"skipped: general skip policy suppresses "
                    f"{choice.strategy.value} for this signature "
                    f"({_skip['failures']} failures, 0 successes)"))
                continue
            # A strategy that has already failed on this exact requirement
            # twice before, in an earlier orchestration run against the same
            # engine's memory, is skipped rather than retried a third time in
            # the same order — this is memory actually changing behaviour,
            # not just being accumulated. It is a skip, not a permanent ban:
            # a fresh orchestration run (fresh engine, fresh memory) tries
            # every strategy again, and even within one run a dependency
            # acquired in the meantime can change what COMPOSE can reach.
            # 2026-09-14: only failures recorded under the CURRENT search
            # policy count. A policy repair makes earlier failures stale
            # evidence — without this scoping, the repair that fixes a goal
            # still can't retry it because the memory-informed skip fires
            # first (observed directly on the plink growth objective).
            _sctx = self._strategy_failure_context()
            prior_failures = [
                h for h in self.engine.failure_memory.history_for(node.name, limit=50)
                if f":{choice.strategy.value}:" in h.detail and h.context == _sctx]
            if len(prior_failures) >= 2:
                result.attempts.append(AttemptRecord(
                    node.name, choice.strategy.value, False,
                    f"skipped: failed {len(prior_failures)} times before for "
                    f"this exact requirement (memory-informed)"))
                continue

            if choice.strategy == Strategy.SEMANTIC_INTERPRETATION:
                diag_start = time.perf_counter_ns()
                target = dict(getattr(spec, "acquisition_target", None) or {})
                semantic_gap = self.engine.semantic_evidence_gaps.record_open(
                    spec.description, target)
                semantic_gap_data = semantic_gap.as_dict()
                if semantic_gap_data not in result.semantic_evidence_gaps:
                    result.semantic_evidence_gaps.append(semantic_gap_data)
                result.attempts.append(AttemptRecord(
                    node.name, choice.strategy.value, False,
                    "independent semantic evidence is insufficient; recorded "
                    "diagnostic without selecting a source or interpretation"))
                _record_attempt(choice.strategy.value, False,
                                time.perf_counter_ns() - diag_start)
                continue

            handler = {
                Strategy.COMPOSE: self._try_compose,
                Strategy.GENERATE: self._try_generate,
                Strategy.EXPERIMENT: self._try_experiment,
                Strategy.DELEGATE: self._try_delegate,
                Strategy.RETRIEVE: self._try_retrieve,
            }.get(choice.strategy)
            if handler is None:
                continue
            # Cost is a real measured quantity per attempt (wall-clock
            # nanoseconds for this handler call), never a per-strategy
            # constant: two attempts of the same strategy record different
            # costs.
            attempt_start = time.perf_counter_ns()
            accepted, detail, capability_id = await handler(node, spec, delegate)
            _record_attempt(choice.strategy.value, accepted,
                            time.perf_counter_ns() - attempt_start)
            result.attempts.append(AttemptRecord(
                node.name, choice.strategy.value, accepted, detail, capability_id))
            if not accepted:
                self.engine.failure_memory.record(
                    goal=node.name, error=(
                        f"acquisition:{node.name}:{choice.strategy.value}: {detail}"),
                    context=self._strategy_failure_context())
            if accepted:
                return signature, True
        return signature, False

    # -- strategies -----------------------------------------------------------
    async def _try_higher_order_map(self, node, spec) -> Tuple[bool, str, str]:
        """Map an acquired capability over a list, induced from evidence.

        2026-09-22 (F3 R11): genuine higher-order reuse was absent -- no
        route applied an already-acquired capability elementwise. This probe
        is evidence-driven, never goal-text: it fires only when every worked
        example maps a list to a same-length list, and only for an acquired
        capability whose own registered behavior maps each input element to
        the corresponding output element on every example. The admitted
        artifact is a plan (map + $partial over the acquired op), so the
        dependency is explicit in the plan graph and participates in
        revoke/recover lifecycle and the boot audit. Same admission bar as
        other compositions: behavioral check on the requirement's examples,
        independent admission, and the held-out gate.
        """
        if not spec.examples:
            return False, "higher-order map probe needs examples", ""
        input_names = list(
            spec.input_names or sorted(spec.examples[0][0].keys()))
        if len(input_names) != 1:
            return False, "higher-order map probe needs a single input", ""
        key = input_names[0]
        try:
            for args, expect in spec.examples:
                in_v = dict(args).get(key)
                if not (isinstance(in_v, list) and isinstance(expect, list)
                        and len(in_v) == len(expect)):
                    raise ValueError("shape")
        except ValueError:
            return False, ("higher-order map probe needs "
                           "list->same-length-list examples"), ""
        try:
            reg_names = list(self.engine.primitives.names())
        except Exception:
            reg_names = []
        try:
            code_names = {r.get("name")
                          for r in self.engine.acquired_code.all()}
        except Exception:
            code_names = set()
        from swarm_engine.primitives.core import Effect
        cands = []
        for name in sorted(reg_names):
            if not (name.startswith("acquired.") or name in code_names):
                continue
            prim = self.engine.primitives.get(name)
            if prim is None:
                continue
            try:
                params = list(prim.inputs.keys())
            except Exception:
                continue
            if len(params) != 1:
                continue
            try:
                effs = tuple(getattr(prim, "effects", None) or ())
                if effs and Effect.PURE not in effs:
                    continue
            except Exception:
                pass
            cands.append((name, prim, params[0]))
        matched = []
        for name, prim, pname in cands:
            try:
                for args, expect in spec.examples:
                    got = [prim.fn(**{pname: e})
                           for e in dict(args)[key]]
                    if got != expect:
                        break
                else:
                    matched.append((name, prim, pname))
            except Exception:
                continue
        if not matched:
            return False, ("no acquired capability maps elements "
                           "for higher-order map"), ""
        name, prim, pname = matched[0]
        plan = {
            "params": {key: "any"},
            "steps": [{
                "id": "mapped", "op": "map",
                "args": {
                    "items": {"$param": key},
                    "fn": {"$partial": {"op": name, "bound": {},
                                       "free": [pname]}},
                },
            }],
            "output": {"$step": "mapped"},
        }
        if not self.engine.composer.analyze(plan).ok:
            return False, "higher-order map plan does not type-check", ""
        for args, expect in spec.examples:
            run = self.engine.composer.execute_sync(plan, dict(args))
            if not (run.get("success") and run.get("value") == expect):
                return False, (
                    "higher-order map fails behavioural check on "
                    f"{args}: got {run.get('value')!r}"), ""
        from swarm_engine.synthesis.admission import SmokeTest
        smoke = SmokeTest(args=dict(spec.examples[0][0]),
                          expect=spec.examples[0][1])
        verdict = self.engine.admission.admit(
            spec.description or node.name, plan, smoke=smoke, name=node.name)
        if not verdict.ok:
            return False, f"admission refused: {verdict.stage}", ""
        self.engine.capabilities.bind_goal(spec.description,
                                           verdict.capability_id)
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        ok, detail = self._heldout_admission_gate(spec, verdict.capability_id)
        if not ok:
            self._reject_capability(verdict.capability_id, detail)
            return False, f"held-out admission rejected: {detail}", ""
        extra = ""
        if len(matched) > 1:
            extra = f"; {len(matched) - 1} other acquired fit"
        return True, (f"higher-order map of {name} "
                      f"({detail}){extra}"), verdict.capability_id

    async def _try_acquired_pipeline(self, node, spec):
        """Compose acquired unary capabilities into a pipeline that
        matches the requirement examples.

        Generic: no goal names. Every acquired family==acquired unary is
        a layer. Chains of length 2..8 are searched; examples select the
        survivor. Longest exact match wins so an 8-deep chain is not
        shadowed by a coincidental pair.
        """
        examples = list(spec.examples or [])
        if len(examples) < 1:
            return False, "no examples for acquired pipeline", ""
        first_args = dict(examples[0][0])
        if len(first_args) != 1:
            return False, "acquired pipeline only for unary goals", ""
        param = next(iter(first_args.keys()))

        def _eq(got, expect):
            if got == expect:
                return True
            try:
                return isinstance(got, (int, float)) and isinstance(expect, (int, float)) and float(got) == float(expect)
            except Exception:
                return False

        unaries = []
        for name in self.engine.primitives.names():
            prim = self.engine.primitives.get(name)
            if prim is None or getattr(prim, "family", "") != "acquired":
                continue
            req = [k for k, v in (prim.inputs or {}).items() if not getattr(v, "optional", False)]
            if len(req) != 1:
                continue
            unaries.append((name, prim, req[0]))
        if len(unaries) < 2:
            return False, "fewer than two acquired unaries", ""

        def _eval_chain(chain, raw):
            val = raw
            for _n, prim, key in chain:
                val = prim.fn(**{key: val})
            return val

        def _fits(chain):
            for args, expect in examples:
                try:
                    got = _eval_chain(chain, args.get(param))
                except Exception:
                    return False
                if not _eq(got, expect):
                    return False
            return True

        matched = []
        # length-2 first (cheap), then DFS up to 8
        for a in unaries:
            for b in unaries:
                if a[0] == b[0]:
                    continue
                chain = [a, b]
                if _fits(chain):
                    matched.append(chain)
        max_len = 9 if len(unaries) <= 16 else 3
        def dfs(chain):
            if len(chain) >= max_len:
                return
            used = {c[0] for c in chain}
            for u in unaries:
                if u[0] in used:
                    continue
                nxt = chain + [u]
                if _fits(nxt):
                    matched.append(nxt)
                # continue extending even if prefix does not match final
                # examples (intermediates are not the target)
                dfs(nxt)
        if max_len > 2:
            budget = {"n": 0}
            def dfs_b(chain):
                budget["n"] += 1
                if budget["n"] > 4000 or len(chain) >= max_len:
                    return
                used = {c[0] for c in chain}
                for u in unaries:
                    if u[0] in used:
                        continue
                    nxt = chain + [u]
                    if _fits(nxt):
                        matched.append(nxt)
                    dfs_b(nxt)
            for u in unaries:
                dfs_b([u])
        if not matched:
            return False, "no acquired unary pipeline fits examples", ""
        matched.sort(key=len, reverse=True)
        chain = matched[0]
        steps = []
        for i, (name, _p, key) in enumerate(chain):
            sid = f"s{i+1}"
            src = {"$param": param} if i == 0 else {"$step": f"s{i}"}
            steps.append({"id": sid, "op": name, "args": {key: src}})
        plan = {
            "name": "acquired_pipeline",
            "params": {param: "any"},
            "steps": steps,
            "output": {"$step": steps[-1]["id"]},
        }
        from swarm_engine.synthesis.admission import SmokeTest
        smoke = SmokeTest(args=dict(examples[0][0]), expect=examples[0][1])
        verdict = self.engine.admission.admit(spec.description, plan, smoke=smoke)
        if not verdict.ok:
            return False, f"pipeline admission refused: {verdict.stage}", ""
        try:
            self.engine.capabilities.bind_goal(spec.description, verdict.capability_id)
        except Exception:
            pass
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        try:
            self.engine.capabilities.bind_goal(node.name, verdict.capability_id)
        except Exception:
            pass
        try:
            prim_name = f"acquired.{verdict.capability_id}"
            src = self.engine.primitives.get(prim_name)
            if src is not None and self.engine.primitives.get(node.name) is None:
                from swarm_engine.primitives.core import Primitive
                self.engine.primitives.register(Primitive(
                    name=node.name,
                    family=getattr(src, "family", "acquired"),
                    fn=src.fn,
                    inputs=dict(getattr(src, "inputs", {}) or {}),
                    output=getattr(src, "output", None),
                    effects=tuple(getattr(src, "effects", ()) or ()),
                    doc=f"alias of {prim_name} for node {node.name}",
                ), overwrite=True)
        except Exception:
            pass
        names = "→".join(c[0] for c in chain)
        extra = f"; {len(matched)-1} other pipelines fit" if len(matched) > 1 else ""
        return True, f"acquired pipeline {names} depth={len(chain)}{extra}", verdict.capability_id

    async def _try_compose(self, node, spec, delegate) -> Tuple[bool, str, str]:
        # Structural pair-assembly: the requirement is a decomposition
        # parent whose children were acquired as primitives earlier in this
        # run (or a previous one). The pairing plan is constructed from the
        # requirement's own structural metadata -- which children, what
        # inputs, how to combine -- never from goal text, and the resulting
        # pairing is behaviorally validated on the parent's examples below,
        # not trusted because it was constructed.
        assembly = dict(getattr(spec, "constraints", None) or {}).get(
            "output_decomposition")
        if isinstance(assembly, dict):
            return await self._try_pair_assembly(node, spec, assembly)
        # Behavioral decomposition parents (P8): assemble from the
        # contract's family/op/children, validated exactly on the parent's
        # examples. Dispatched here -- after structural pair-assembly,
        # before generic template composition -- so the validated
        # behavioral assembly wins over coincidental skeletons.
        bdecomp = dict(getattr(spec, "constraints", None) or {}).get(
            "behavioral_decomposition")
        if isinstance(bdecomp, dict):
            return await self._assemble_behavioral_parent(node, spec, bdecomp)
        pipe = await self._try_acquired_pipeline(node, spec)
        if pipe[0]:
            return pipe
        ho = await self._try_higher_order_map(node, spec)
        if ho[0]:
            return ho
        proposal = self.engine.planner.best(spec.description)
        if proposal is None or not proposal.strategy.startswith("template"):
            return False, "no deliberate composition found", ""
        if not self.engine.composer.analyze(proposal.plan).ok:
            return False, "composition does not type-check", ""
        # Generic integrity guard, the same one the gap reasoner applies in
        # its check #2: a template proposal that is not a semantically
        # sufficient match for the requirement is a coincidental skeleton,
        # not a deliberate composition. Found directly: planner.best() still
        # returns its best-scoring template even when nothing scores well, so
        # without this check an unrelated "aggregate/data.length" template
        # can be admitted against a conditional requirement without any
        # behavioral validation — the exact impostor failure the gap path
        # already refuses. This is not a per-goal branch: every COMPOSE
        # attempt is scored the same way against the requirement's own text.
        match = self.reasoner._matcher.score(
            spec.description, description=" ".join(proposal.ops_used),
            ops_used=proposal.ops_used, strategy=proposal.strategy)
        if not match.sufficient:
            return False, (f"template {proposal.strategy!r} is not a "
                           f"semantically sufficient match for the requirement "
                           f"(score {match.score:.2f}); refused as a "
                           f"coincidental skeleton"), ""
        # The capability's persisted .goal field and its goal-binding key
        # both need real text to be useful later — found directly: binding
        # under node.name used the auto-derived node label (frequently the
        # generic catch-all "unclassified" whenever a specific name can't
        # be inferred), which not only carries no meaning for a future
        # semantic match, it collides across every unrelated requirement
        # that also gets that same generic label. spec.description is the
        # requirement's own real text, unique to what was actually asked
        # for, regardless of what the graph node happened to be named.
        # Behavioural check on all leaf examples before admission — template
        # semantic match alone is insufficient (e.g. sum matching both
        # "part 1" and "part 2" of a list decomposition).
        if spec.examples:
            for args, expect in spec.examples:
                run = self.engine.composer.execute_sync(proposal.plan, dict(args))
                if not (run.get("success") and run.get("value") == expect):
                    return False, (f"composition {proposal.ops_used} fails "
                                   f"behavioural check on {args}: "
                                   f"got {run.get('value')!r} expected {expect!r}"), ""
        from swarm_engine.synthesis.admission import SmokeTest
        smoke = None
        if spec.examples:
            smoke = SmokeTest(args=dict(spec.examples[0][0]), expect=spec.examples[0][1])
        verdict = self.engine.admission.admit(spec.description, proposal.plan, smoke=smoke)
        if not verdict.ok:
            return False, f"admission refused: {verdict.stage}", ""
        self.engine.capabilities.bind_goal(spec.description, verdict.capability_id)
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        return True, f"composed from {proposal.ops_used}", verdict.capability_id

    async def _try_pair_assembly(self, node, spec,
                                 assembly: Dict[str, Any]
                                 ) -> Tuple[bool, str, str]:
        """Compose a decomposition parent from its acquired children.

        Builds the single generic re-assembly plan this decomposition shape
        allows: call each child (now a registered primitive) on the shared
        inputs, then combine the sub-results with tuple/list/dict
        construction. Every step is structural -- child names, input names,
        and the combination kind all come from the requirement's
        `output_decomposition` constraint, which itself was derived from
        example SHAPE, never goal words. The assembled pairing is executed
        against ALL of the parent's worked examples before admission: a
        pairing that does not reproduce them is refused, not trusted.
        """
        children = list(assembly.get("children") or [])
        kind = assembly.get("kind") or "tuple"
        if not children:
            return False, "output_decomposition names no children", ""
        # Gap-A: remap through the parent's structural metadata. A child
        # satisfied by behavioral discovery executes under its DISCOVERED
        # primitive name, not this decomposition's node name (a reworded
        # goal decomposes to new node names while the admitted primitive
        # keeps its old one). Children absent from the map keep their node
        # names: freshly acquired children register under exactly those.
        # The availability check below fail-closes on any name that is
        # not a registered primitive.
        resolved = assembly.get("resolved_primitives") or {}
        if resolved:
            children = [resolved.get(c, c) for c in children]
        if not spec.examples:
            return False, ("no parent examples; the assembled pairing cannot "
                           "be behaviorally validated"), ""
        # Structural pair-assembly may carry optional const_bindings when
        # a shared schema is reused; normally empty. Never reference the
        # behavioral `bdecomp` name here.
        const_bindings_early = dict(assembly.get("const_bindings") or {})
        orig_early = list(assembly.get("children")
                          or assembly.get("child_refs") or [])
        missing = []
        for i, c in enumerate(children):
            orig = orig_early[i] if i < len(orig_early) else c
            if orig in const_bindings_early or c == "__const__":
                continue  # const inlined; no primitive required
            if self.engine.primitives.get(c) is None:
                missing.append(c)
        if missing:
            return False, (f"prerequisite(s) {missing} are not available "
                           f"primitives yet; the parent cannot be assembled "
                           f"before its parts"), ""

        parent_inputs = list(spec.input_names)
        steps = []
        for i, child in enumerate(children):
            prim = self.engine.primitives.get(child)
            child_inputs = list(prim.inputs.keys())
            unmapped = [n for n in child_inputs if n not in parent_inputs]
            if unmapped:
                return False, (f"prerequisite {child!r} needs input(s) "
                               f"{unmapped} the parent does not provide; "
                               f"refusing rather than inventing values"), ""
            steps.append({"id": f"part{i}", "op": child,
                          "args": {n: {"$param": n} for n in child_inputs}})
        part_refs = [{"$step": f"part{i}"} for i in range(len(children))]
        if kind == "dict":
            keys = list(assembly.get("keys") or [])
            if len(keys) != len(children):
                return False, ("output_decomposition keys do not match the "
                                "child list; refusing a miswired assembly"), ""
            output = {"$dict": {k: part_refs[i] for i, k in enumerate(keys)}}
        elif kind == "list":
            output = {"$list": part_refs}
        else:
            output = {"$tuple": part_refs}
        plan = {"name": node.name,
                "params": {n: "any" for n in parent_inputs},
                "steps": steps, "output": output}

        if not self.engine.composer.analyze(plan).ok:
            return False, "assembled pairing plan does not type-check", ""
        from swarm_engine.acquisition.semantic import Case
        cases = [Case(args=dict(a), expect=v) for a, v in spec.examples]
        bad = []
        for case in cases:
            run = self.engine.composer.execute_sync(
                plan, dict(case.args), skip_check=True)
            passed, why = case.judge(
                bool(run.get("success")), run.get("value"),
                str(run.get("error") or ""))
            if not passed:
                bad.append((case.args, run.get("value"), case.expect, why))
                break
        if bad:
            args, got, want, why = bad[0]
            return False, (f"assembled pairing failed behavioral validation "
                           f"on the parent's examples: {args} -> {got!r} "
                           f"({why}; expected {want!r})"), ""
        # No semantic-template guard here: the pairing was constructed from
        # the requirement's own structural metadata and then behaviorally
        # validated on every parent example. A coincidental skeleton cannot
        # reproduce exact outputs; the examples are the evidence.
        from swarm_engine.synthesis.admission import SmokeTest
        first_args, first_expect = spec.examples[0]
        verdict = self.engine.admission.admit(
            spec.description, plan,
            smoke=SmokeTest(args=dict(first_args), expect=first_expect))
        if not verdict.ok:
            return False, f"admission refused: {verdict.stage}", ""
        self.engine.capabilities.bind_goal(spec.description, verdict.capability_id)
        # Multi-level assembly: a pair-assembled parent references its
        # children via acquired.* primitives, so it is NOT portable
        # standalone and admission will not register it as a primitive.
        # But a grandparent in a 3+-level decomposition graph needs to
        # address this parent by name. Register it explicitly here --
        # orchestrator-specific (parent assembly), not affecting the
        # synthesis reuse path (which only sees portable capabilities,
        # preserving the reification observer's visibility into recurring
        # sub-computations).
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        n = len(spec.examples)
        return True, (f"pair-assembled from {children}; pairing validated on "
                      f"{n}/{n} parent examples then admitted"), verdict.capability_id


    def _materialize_partial_residual(
            self, base_op: str, arg_bind: dict, parent_inputs: list):
        """Persist a first-class residual-arity capability for partial app.

        Given a base acquired/promoted op and an arg_bind that mixes
        ``__lit__:<input>`` literal binds with free remaps to parent
        params, admit a fewer-arity capability whose plan calls ``base_op``
        with the literals fixed. Register it as an acquired primitive so
        later hierarchical reuse can match it at the residual arity
        without re-binding. Deterministic plan fingerprint => re-admit
        reuses. Returns ``(prim_name, free_args_for_parent_step)`` or
        None on failure (caller keeps inline lit binds).
        """
        lit_binds = {}
        free_map = {}  # prim_input -> parent_param
        for pin, val in arg_bind.items():
            if isinstance(pin, str) and pin.startswith("__lit__:"):
                real = pin.split(":", 1)[1]
                lit = val
                if isinstance(lit, str):
                    try:
                        lit = int(lit)
                    except ValueError:
                        try:
                            lit = float(lit)
                        except ValueError:
                            pass
                lit_binds[real] = lit
            else:
                free_map[pin] = val
        if not lit_binds or not free_map:
            return None
        for parent_col in free_map.values():
            if parent_col not in parent_inputs:
                return None
        residual_params = {pin: "any" for pin in free_map}
        call_args = {pin: lit for pin, lit in lit_binds.items()}
        for pin in free_map:
            call_args[pin] = {"$param": pin}
        plan = {
            "name": "partial_residual",
            "params": residual_params,
            "steps": [{"id": "r0", "op": base_op, "args": call_args}],
            "output": {"$step": "r0"},
        }
        bind_sig = ",".join(f"{k}={lit_binds[k]!r}"
                            for k in sorted(lit_binds))
        free_sig = ",".join(sorted(free_map.keys()))
        goal = (f"partial residual of {base_op} "
                f"bind[{bind_sig}] free[{free_sig}]")
        smoke_args = {pin: 1 for pin in free_map}
        try:
            run = self.engine.composer.execute_sync(
                plan, dict(smoke_args), skip_check=True)
        except Exception:
            return None
        if not run.get("success"):
            return None
        expect = run.get("value")
        from swarm_engine.synthesis.admission import SmokeTest
        try:
            verdict = self.engine.admission.admit(
                goal, plan,
                smoke=SmokeTest(args=dict(smoke_args), expect=expect))
        except Exception:
            return None
        if not getattr(verdict, "ok", False):
            return None
        try:
            self.engine.capabilities.bind_goal(goal, verdict.capability_id)
        except Exception:
            pass
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        rname = f"acquired.{verdict.capability_id}"
        if self.engine.primitives.get(rname) is None:
            acq_ids = (getattr(self.engine.primitives,
                               "_acquired_capability_ids", None) or {})
            for registered_name, cid in acq_ids.items():
                if cid == verdict.capability_id:
                    rname = registered_name
                    break
            else:
                return None
        alias_name = (
            "residual_"
            + "".join(ch if ch.isalnum() else "_" for ch in base_op)[:40]
            + "_"
            + "".join(f"{k}{lit_binds[k]}" for k in sorted(lit_binds))
        )
        if self.engine.primitives.get(alias_name) is None:
            try:
                from swarm_engine.primitives.core import Primitive
                src = self.engine.primitives.get(rname)
                alias = Primitive(
                    name=alias_name,
                    family=getattr(src, "family", "acquired"),
                    fn=src.fn,
                    inputs=dict(getattr(src, "inputs", {}) or {}),
                    output=getattr(src, "output", None),
                    effects=tuple(getattr(src, "effects", ()) or ()),
                    doc=f"partial residual alias of {rname} ({goal})",
                )
                self.engine.primitives.register(alias, overwrite=True)
                ti = getattr(self.engine.primitives,
                             "_acquired_capability_ids", None)
                if isinstance(ti, dict):
                    ti[alias_name] = verdict.capability_id
                rname = alias_name
            except Exception:
                pass
        else:
            rname = alias_name
        free_args = {pin: {"$param": free_map[pin]} for pin in free_map}
        return (rname, free_args)

    async def _assemble_structural_wrap(self, node, spec, bdecomp, children):
        """Assemble structural_wrap(parent = kv(key, child) | lift(child)).

        Generic nested container reconstruction: one child produces the
        nested value; the wrap op re-applies the singleton container.
        """
        op = bdecomp.get("op") or {}
        if not isinstance(op, dict) or not op.get("op"):
            return False, f"structural_wrap needs dict op, got {op!r}", ""
        wrap_op = op.get("op")
        if len(children) != 1:
            return False, (
                f"structural_wrap needs exactly 1 child "
                f"(got {len(children)})"), ""
        if not spec.examples:
            return False, ("no parent examples; structural wrap cannot "
                           "be validated"), ""
        resolved = bdecomp.get("resolved_primitives") or {}
        child = children[0]
        child_prim = resolved.get(child, child)
        const_bindings = dict(bdecomp.get("const_bindings") or {})
        param_bindings = dict(bdecomp.get("param_bindings") or {})
        reuse_arg_bindings = dict(bdecomp.get("reuse_arg_bindings") or {})
        orig_children = list(bdecomp.get("children")
                             or bdecomp.get("child_refs") or [])
        orig = orig_children[0] if orig_children else child
        parent_inputs = list(spec.input_names)

        steps = []
        # Resolve the single child to a step/literal ref.
        if orig in const_bindings:
            child_ref = ("const", const_bindings[orig])
        elif child_prim == "identity" and orig in param_bindings:
            col = param_bindings[orig]
            if col not in parent_inputs:
                return False, f"wrap param child needs input {col!r}", ""
            id_prim = self.engine.primitives.get("identity")
            if id_prim is None:
                return False, "identity primitive missing", ""
            id_ins = list(id_prim.inputs.keys())
            if len(id_ins) != 1:
                return False, "identity not unary", ""
            steps.append({
                "id": "wrap_child", "op": "identity",
                "args": {id_ins[0]: {"$param": col}},
            })
            child_ref = ("step", "wrap_child")
        else:
            if self.engine.primitives.get(child_prim) is None:
                # v41: hierarchical wrap child_refs (..._w0) may not equal
                # the rebound/acquired stem name. Resolve via stem strip,
                # then via exact behavioral match on the wrap child's
                # projected examples against acquired unaries.
                alt = None
                if child_prim.endswith("_w0"):
                    stem = child_prim[:-3]
                    if self.engine.primitives.get(stem) is not None:
                        alt = stem
                if alt is None:
                    # Projected examples for this child, if carried on decomp
                    proj = None
                    for ch in (bdecomp.get("children_meta") or []):
                        if isinstance(ch, dict) and ch.get("name") == child:
                            proj = ch.get("projected_examples")
                            break
                    if not proj:
                        # Fall back: any acquired unary that realizes parent
                        # peel — last-acquired structural sibling heuristic
                        # replaced by exact example match when available.
                        proj = None
                    if proj:
                        want = [o for _, o in proj]
                        for pname in sorted(self.engine.primitives.names()):
                            try:
                                prim_c = self.engine.primitives.get(pname)
                                if getattr(prim_c, "family", "") not in (
                                        "acquired", "promoted"):
                                    continue
                                pins = [k for k, v in prim_c.inputs.items()
                                        if not getattr(v, "optional", False)]
                                if len(pins) != 1:
                                    continue
                                pin = pins[0]
                                got = []
                                ok = True
                                for args, _exp in proj:
                                    try:
                                        got.append(prim_c.fn(**{pin: args[pin] if pin in args else next(iter(args.values()))}))
                                    except Exception:
                                        ok = False
                                        break
                                if ok and got == want:
                                    alt = pname
                                    break
                            except Exception:
                                continue
                    if alt is None and child_prim.endswith("_w0") and spec.examples:
                        # Derive the peeled child vector from parent examples
                        # via the wrap op, then find an acquired unary that
                        # realizes it. Generic: no name-stem coupling.
                        wrap_op = (bdecomp.get("op") or {}).get("op")
                        peeled = []
                        try:
                            for args, expect in spec.examples:
                                if wrap_op == "kv":
                                    key = (bdecomp.get("op") or {}).get("key")
                                    peeled.append((dict(args), expect[key]))
                                elif wrap_op in ("lift", "lift_tuple", "lift_set", "to_bytes"):
                                    peeled.append((dict(args), expect[0]))
                                else:
                                    peeled = []
                                    break
                        except Exception:
                            peeled = []
                        if peeled:
                            want = [o for _, o in peeled]
                            for pname in sorted(self.engine.primitives.names()):
                                try:
                                    prim_c = self.engine.primitives.get(pname)
                                    if getattr(prim_c, "family", "") not in (
                                            "acquired", "promoted"):
                                        continue
                                    pins = [k for k, v in prim_c.inputs.items()
                                            if not getattr(v, "optional", False)]
                                    if len(pins) != 1:
                                        continue
                                    pin = pins[0]
                                    got = []
                                    ok = True
                                    for args, _e in peeled:
                                        try:
                                            aval = args[pin] if pin in args else next(iter(args.values()))
                                            got.append(prim_c.fn(**{pin: aval}))
                                        except Exception:
                                            ok = False
                                            break
                                    if ok and got == want:
                                        alt = pname
                                        break
                                except Exception:
                                    continue
                if alt is not None:
                    child_prim = alt
                    resolved[child] = alt
            if self.engine.primitives.get(child_prim) is None:
                return False, (
                    f"wrap child primitive {child_prim!r} not available"), ""
            prim = self.engine.primitives.get(child_prim)
            pins = list(prim.inputs.keys())
            arg_bind = reuse_arg_bindings.get(orig) or {}
            args = {}
            if arg_bind:
                for pin, val in arg_bind.items():
                    if isinstance(pin, str) and pin.startswith("__lit__:"):
                        real = pin.split(":", 1)[1]
                        lit = val
                        if isinstance(lit, str):
                            try:
                                lit = int(lit)
                            except ValueError:
                                try:
                                    lit = float(lit)
                                except ValueError:
                                    pass
                        args[real] = lit
                    else:
                        if val not in parent_inputs:
                            return False, (
                                f"wrap child bind needs parent input "
                                f"{val!r}"), ""
                        args[pin] = {"$param": val}
            else:
                for pin in pins:
                    if pin not in parent_inputs:
                        return False, (
                            f"wrap child {child_prim!r} needs input "
                            f"{pin!r}"), ""
                    args[pin] = {"$param": pin}
            steps.append({
                "id": "wrap_child", "op": child_prim, "args": args,
            })
            child_ref = ("step", "wrap_child")

        if wrap_op == "kv":
            key = op.get("key")
            if key is None:
                return False, "structural_wrap kv needs key", ""
            if self.engine.primitives.get("kv") is None:
                return False, "kv primitive not registered", ""
            val_arg = (child_ref[1] if child_ref[0] == "const"
                       else {"$step": child_ref[1]})
            steps.append({
                "id": "wrap0", "op": "kv",
                "args": {"key": key, "value": val_arg},
            })
            prev = "wrap0"
        elif wrap_op in ("lift", "lift_tuple", "lift_set", "to_bytes"):
            if self.engine.primitives.get(wrap_op) is None:
                return False, f"{wrap_op} primitive not registered", ""
            lift_prim = self.engine.primitives.get(wrap_op)
            lift_ins = list(lift_prim.inputs.keys())
            if len(lift_ins) != 1:
                return False, f"{wrap_op} not unary", ""
            val_arg = (child_ref[1] if child_ref[0] == "const"
                       else {"$step": child_ref[1]})
            steps.append({
                "id": "wrap0", "op": wrap_op,
                "args": {lift_ins[0]: val_arg},
            })
            prev = "wrap0"
        else:
            return False, f"unsupported structural_wrap op {wrap_op!r}", ""

        plan = {
            "name": node.name,
            "params": {n: "any" for n in parent_inputs},
            "steps": steps,
            "output": {"$step": prev},
        }
        if not self.engine.composer.analyze(plan).ok:
            return False, "assembled structural wrap plan does not type-check", ""
        from swarm_engine.acquisition.semantic import Case
        cases = [Case(args=dict(a), expect=v) for a, v in spec.examples]
        bad = []
        for case in cases:
            run = self.engine.composer.execute_sync(
                plan, dict(case.args), skip_check=True)
            if not run.get("success") or run.get("value") != case.expect:
                bad.append((case.args, run.get("value"), case.expect))
                if len(bad) >= 3:
                    break
        if bad:
            return False, (
                f"structural wrap failed exact validation on parent "
                f"examples (e.g. got={bad[0][1]!r} expect={bad[0][2]!r})"), ""
        from swarm_engine.synthesis.admission import SmokeTest
        from swarm_engine.synthesis.admission import SmokeTest
        first_args, first_expect = spec.examples[0]
        try:
            verdict = self.engine.admission.admit(
                spec.description, plan,
                smoke=SmokeTest(args=dict(first_args), expect=first_expect))
        except Exception as exc:
            return False, f"structural wrap admission error: {exc}", ""
        if not getattr(verdict, "ok", False):
            return False, (
                f"structural wrap admission rejected: "
                f"{getattr(verdict, 'reason', getattr(verdict, 'stage', verdict))}"), ""
        self.engine.capabilities.bind_goal(
            spec.description, verdict.capability_id)
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        ok, detail = self._heldout_admission_gate(spec, verdict.capability_id)
        if not ok:
            self._reject_capability(verdict.capability_id, detail)
            return False, f"held-out admission rejected: {detail}", ""
        n = len(spec.examples)
        return True, (
            f"structurally wrapped ({wrap_op}) from {children}; "
            f"validated on {n}/{n} parent examples then admitted"), (
                verdict.capability_id)


    async def _assemble_structural_product(self, node, spec, bdecomp, children):
        """Assemble structural_product via lift+append / lift_tuple+tuple_concat.

        Each child produces one positional slot; pack_list rebuilds
        append(lift(c0), c1, ...) and pack_tuple rebuilds nested
        tuple_concat(lift_tuple(...), ...). Generic — no per-goal code.
        """
        op = bdecomp.get("op") or {}
        if not isinstance(op, dict) or not op.get("op"):
            return False, f"structural_product needs dict op, got {op!r}", ""
        pack_op = op.get("op")
        if pack_op not in ("pack_list", "pack_tuple",
                           "pack_set", "pack_frozenset"):
            return False, f"unsupported structural_product op {pack_op!r}", ""
        if not (2 <= len(children) <= 7):
            return False, (
                f"structural_product needs 2..7 children "
                f"(got {len(children)})"), ""
        if not spec.examples:
            return False, ("no parent examples; structural product cannot "
                           "be validated"), ""
        resolved = bdecomp.get("resolved_primitives") or {}
        const_bindings = dict(bdecomp.get("const_bindings") or {})
        param_bindings = dict(bdecomp.get("param_bindings") or {})
        reuse_arg_bindings = dict(bdecomp.get("reuse_arg_bindings") or {})
        orig_children = list(bdecomp.get("children")
                             or bdecomp.get("child_refs") or [])
        parent_inputs = list(spec.input_names)

        steps = []
        slot_refs = []

        for idx, child in enumerate(children):
            orig = orig_children[idx] if idx < len(orig_children) else child
            child_prim = resolved.get(child, child)
            if orig in const_bindings:
                slot_refs.append(("const", const_bindings[orig]))
                continue
            if child_prim == "identity" and orig in param_bindings:
                col = param_bindings[orig]
                if col not in parent_inputs:
                    return False, f"product param child needs input {col!r}", ""
                id_prim = self.engine.primitives.get("identity")
                if id_prim is None:
                    return False, "identity primitive missing", ""
                id_ins = list(id_prim.inputs.keys())
                if len(id_ins) != 1:
                    return False, "identity not unary", ""
                sid = f"prod_child_{idx}"
                steps.append({
                    "id": sid, "op": "identity",
                    "args": {id_ins[0]: {"$param": col}},
                })
                slot_refs.append(("step", sid))
                continue
            if self.engine.primitives.get(child_prim) is None:
                alt = None
                peeled = []
                try:
                    for args, expect in spec.examples:
                        if not isinstance(expect, (list, tuple)):
                            peeled = []
                            break
                        if idx >= len(expect):
                            peeled = []
                            break
                        peeled.append((dict(args), expect[idx]))
                except Exception:
                    peeled = []
                if peeled:
                    want = [o for _, o in peeled]
                    for pname in sorted(self.engine.primitives.names()):
                        try:
                            prim_c = self.engine.primitives.get(pname)
                            if getattr(prim_c, "family", "") not in (
                                    "acquired", "promoted"):
                                continue
                            pins = [k for k, v in prim_c.inputs.items()
                                    if not getattr(v, "optional", False)]
                            if len(pins) != 1:
                                continue
                            if not parent_inputs:
                                continue
                            col = parent_inputs[0]
                            got = []
                            ok = True
                            for args, _e in peeled:
                                try:
                                    got.append(prim_c.fn(
                                        **{pins[0]: args[col]}))
                                except Exception:
                                    ok = False
                                    break
                            if ok and got == want:
                                alt = pname
                                break
                        except Exception:
                            continue
                if alt is None:
                    return False, (
                        f"product child primitive {child_prim!r} "
                        f"not available"), ""
                child_prim = alt
            prim = self.engine.primitives.get(child_prim)
            pins = [k for k, v in prim.inputs.items()
                    if not getattr(v, "optional", False)]
            args = {}
            bind = reuse_arg_bindings.get(orig) or {}
            if len(pins) == 1:
                col = bind.get(pins[0])
                if col is None:
                    if pins[0] in parent_inputs:
                        col = pins[0]
                    elif len(parent_inputs) == 1:
                        col = parent_inputs[0]
                    else:
                        return False, (
                            f"product child {child_prim!r} needs input "
                            f"binding for {pins[0]!r}"), ""
                if col not in parent_inputs:
                    return False, (
                        f"product child bind needs parent input {col!r}"), ""
                args[pins[0]] = {"$param": col}
            else:
                for p in pins:
                    col = bind.get(p, p if p in parent_inputs else None)
                    if col is None or col not in parent_inputs:
                        return False, (
                            f"product child {child_prim!r} needs bind "
                            f"for {p!r}"), ""
                    args[p] = {"$param": col}
            sid = f"prod_child_{idx}"
            steps.append({"id": sid, "op": child_prim, "args": args})
            slot_refs.append(("step", sid))

        def _ref_arg(ref):
            if ref[0] == "const":
                return ref[1]
            return {"$step": ref[1]}

        if pack_op in ("pack_list", "pack_set", "pack_frozenset"):
            if self.engine.primitives.get("lift") is None:
                return False, "lift primitive not registered", ""
            if self.engine.primitives.get("append") is None:
                return False, "append primitive not registered", ""
            lift_ins = list(self.engine.primitives.get("lift").inputs.keys())
            app_ins = list(self.engine.primitives.get("append").inputs.keys())
            if len(lift_ins) != 1 or len(app_ins) != 2:
                return False, "lift/append arity mismatch", ""
            steps.append({
                "id": "pack0", "op": "lift",
                "args": {lift_ins[0]: _ref_arg(slot_refs[0])},
            })
            prev = "pack0"
            for j in range(1, len(slot_refs)):
                sid = f"pack{j}"
                steps.append({
                    "id": sid, "op": "append",
                    "args": {
                        app_ins[0]: {"$step": prev},
                        app_ins[1]: _ref_arg(slot_refs[j]),
                    },
                })
                prev = sid
        else:
            if self.engine.primitives.get("lift_tuple") is None:
                return False, "lift_tuple primitive not registered", ""
            if self.engine.primitives.get("tuple_concat") is None:
                return False, "tuple_concat primitive not registered", ""
            lt_ins = list(
                self.engine.primitives.get("lift_tuple").inputs.keys())
            tc_ins = list(
                self.engine.primitives.get("tuple_concat").inputs.keys())
            if len(lt_ins) != 1 or len(tc_ins) != 2:
                return False, "lift_tuple/tuple_concat arity mismatch", ""
            lift_ids = []
            for j, ref in enumerate(slot_refs):
                sid = f"tplift{j}"
                steps.append({
                    "id": sid, "op": "lift_tuple",
                    "args": {lt_ins[0]: _ref_arg(ref)},
                })
                lift_ids.append(sid)
            prev = lift_ids[0]
            for j in range(1, len(lift_ids)):
                sid = f"pack{j}"
                steps.append({
                    "id": sid, "op": "tuple_concat",
                    "args": {
                        tc_ins[0]: {"$step": prev},
                        tc_ins[1]: {"$step": lift_ids[j]},
                    },
                })
                prev = sid

        if pack_op in ("pack_set", "pack_frozenset"):
            as_name = "as_set" if pack_op == "pack_set" else "as_frozenset"
            if self.engine.primitives.get(as_name) is None:
                return False, f"{as_name} primitive not registered", ""
            as_ins = list(self.engine.primitives.get(as_name).inputs.keys())
            if len(as_ins) != 1:
                return False, f"{as_name} arity mismatch", ""
            steps.append({
                "id": "as_set_wrap", "op": as_name,
                "args": {as_ins[0]: {"$step": prev}},
            })
            prev = "as_set_wrap"

        plan = {
            "name": node.name,
            "params": {n: "any" for n in parent_inputs},
            "steps": steps,
            "output": {"$step": prev},
        }
        if not self.engine.composer.analyze(plan).ok:
            return False, "assembled structural product plan does not type-check", ""
        from swarm_engine.acquisition.semantic import Case
        cases = [Case(args=dict(a), expect=v) for a, v in spec.examples]
        bad = []
        for case in cases:
            run = self.engine.composer.execute_sync(
                plan, dict(case.args), skip_check=True)
            if not run.get("success") or run.get("value") != case.expect:
                bad.append((case.args, run.get("value"), case.expect))
                if len(bad) >= 3:
                    break
        if bad:
            return False, (
                f"structural product failed exact validation on parent "
                f"examples (e.g. got={bad[0][1]!r} expect={bad[0][2]!r})"), ""
        from swarm_engine.synthesis.admission import SmokeTest
        first_args, first_expect = spec.examples[0]
        try:
            verdict = self.engine.admission.admit(
                spec.description, plan,
                smoke=SmokeTest(args=dict(first_args), expect=first_expect))
        except Exception as exc:
            return False, f"structural product admission error: {exc}", ""
        if not getattr(verdict, "ok", False):
            return False, (
                f"structural product admission rejected: "
                f"{getattr(verdict, 'reason', getattr(verdict, 'stage', verdict))}"), ""
        self.engine.capabilities.bind_goal(
            spec.description, verdict.capability_id)
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        ok, detail = self._heldout_admission_gate(spec, verdict.capability_id)
        if not ok:
            self._reject_capability(verdict.capability_id, detail)
            return False, f"held-out admission rejected: {detail}", ""
        n = len(spec.examples)
        return True, (
            f"structurally packed ({pack_op}) from {children}; "
            f"validated on {n}/{n} parent examples then admitted"), (
                verdict.capability_id)

    async def _assemble_varlen_sequence(self, node, spec, bdecomp, children):
        """Assemble varlen_sequence via repeat(value,count) or foreach-map.

        General length-indexed reconstruction — no per-cardinality branches.
        """
        op = bdecomp.get("op") or {}
        if not isinstance(op, dict) or not op.get("op"):
            return False, f"varlen_sequence needs dict op, got {op!r}", ""
        kind = op.get("op")
        if kind not in ("repeat", "map_elements"):
            return False, f"unsupported varlen_sequence op {kind!r}", ""
        if len(children) != 2:
            return False, (
                f"varlen_sequence needs exactly 2 children "
                f"(got {len(children)})"), ""
        if not spec.examples:
            return False, ("no parent examples; varlen sequence cannot "
                           "be validated"), ""
        resolved = bdecomp.get("resolved_primitives") or {}
        const_bindings = dict(bdecomp.get("const_bindings") or {})
        param_bindings = dict(bdecomp.get("param_bindings") or {})
        reuse_arg_bindings = dict(bdecomp.get("reuse_arg_bindings") or {})
        orig_children = list(bdecomp.get("children")
                             or bdecomp.get("child_refs") or [])
        parent_inputs = list(spec.input_names)

        def _as_arg(ref):
            if ref[0] == "const":
                return ref[1]
            if ref[0] == "param":
                return {"$param": ref[1]}
            if ref[0] == "step":
                return {"$step": ref[1]}
            return None

        def _child_ref(idx):
            """Build steps for child idx; return (steps, ref_tuple_or_err)."""
            child = children[idx]
            orig = orig_children[idx] if idx < len(orig_children) else child
            child_prim = resolved.get(child, child)
            steps_d = []
            if orig in const_bindings:
                return steps_d, ("const", const_bindings[orig])
            if child_prim == "identity" and orig in param_bindings:
                col = param_bindings[orig]
                if col not in parent_inputs:
                    return None, f"varlen param child needs input {col!r}"
                id_prim = self.engine.primitives.get("identity")
                if id_prim is None:
                    return None, "identity primitive missing"
                id_ins = list(id_prim.inputs.keys())
                if len(id_ins) != 1:
                    return None, "identity not unary"
                sid = f"varlen_child_{idx}"
                steps_d.append({
                    "id": sid, "op": "identity",
                    "args": {id_ins[0]: {"$param": col}},
                })
                return steps_d, ("step", sid)
            if orig in param_bindings:
                col = param_bindings[orig]
                if col not in parent_inputs:
                    return None, f"varlen param bind needs {col!r}"
                return steps_d, ("param", col)
            if self.engine.primitives.get(child_prim) is None:
                alt = None
                if kind == "repeat":
                    peeled = []
                    try:
                        for args, expect in spec.examples:
                            if not isinstance(expect, list):
                                peeled = []
                                break
                            if idx == 0:
                                peeled.append(
                                    (dict(args),
                                     expect[0] if expect else None))
                            else:
                                peeled.append((dict(args), len(expect)))
                    except Exception:
                        peeled = []
                    if peeled:
                        want = [o for _, o in peeled]
                        for pname in sorted(self.engine.primitives.names()):
                            try:
                                prim_c = self.engine.primitives.get(pname)
                                if getattr(prim_c, "family", "") not in (
                                        "acquired", "promoted"):
                                    continue
                                pins = [
                                    k for k, v in prim_c.inputs.items()
                                    if not getattr(v, "optional", False)]
                                if len(pins) != 1 or not parent_inputs:
                                    continue
                                col = parent_inputs[0]
                                got, ok = [], True
                                for args, _e in peeled:
                                    try:
                                        got.append(prim_c.fn(
                                            **{pins[0]: args[col]}))
                                    except Exception:
                                        ok = False
                                        break
                                if ok and got == want:
                                    alt = pname
                                    break
                            except Exception:
                                continue
                if alt is None:
                    return None, (
                        f"varlen child primitive {child_prim!r} "
                        f"not available")
                child_prim = alt
            prim = self.engine.primitives.get(child_prim)
            if prim is None:
                return None, f"varlen child {child_prim!r} missing"
            pins = [k for k, v in prim.inputs.items()
                    if not getattr(v, "optional", False)]
            bind = reuse_arg_bindings.get(orig) or {}
            args = {}
            if len(pins) == 1:
                col = bind.get(pins[0])
                if col is None:
                    if pins[0] in parent_inputs:
                        col = pins[0]
                    elif len(parent_inputs) == 1:
                        col = parent_inputs[0]
                    else:
                        return None, (
                            f"varlen child {child_prim!r} needs input "
                            f"binding for {pins[0]!r}")
                if col not in parent_inputs:
                    return None, (
                        f"varlen child bind needs parent input {col!r}")
                args[pins[0]] = {"$param": col}
            else:
                for p in pins:
                    col = bind.get(p, p if p in parent_inputs else None)
                    if col is None or col not in parent_inputs:
                        return None, (
                            f"varlen child {child_prim!r} needs bind "
                            f"for {p!r}")
                    args[p] = {"$param": col}
            sid = f"varlen_child_{idx}"
            steps_d.append({"id": sid, "op": child_prim, "args": args})
            return steps_d, ("step", sid)

        steps = []
        if kind == "repeat":
            if self.engine.primitives.get("repeat") is None:
                return False, "repeat primitive not registered", ""
            r_ins = list(self.engine.primitives.get("repeat").inputs.keys())
            if list(r_ins) != ["value", "count"] and len(r_ins) != 2:
                # accept any 2-input order; prefer names when present
                pass
            if len(r_ins) != 2:
                return False, "repeat arity mismatch", ""
            # Prefer named pins when available
            val_pin = "value" if "value" in r_ins else r_ins[0]
            cnt_pin = "count" if "count" in r_ins else r_ins[1]
            sd0, ref0 = _child_ref(0)
            if sd0 is None:
                return False, ref0, ""
            steps.extend(sd0)
            sd1, ref1 = _child_ref(1)
            if sd1 is None:
                return False, ref1, ""
            steps.extend(sd1)
            v_arg, c_arg = _as_arg(ref0), _as_arg(ref1)
            if v_arg is None or c_arg is None:
                return False, f"repeat refs unresolved {ref0!r} {ref1!r}", ""
            steps.append({
                "id": "varlen_rep", "op": "repeat",
                "args": {val_pin: v_arg, cnt_pin: c_arg},
            })
            prev = "varlen_rep"
        else:
            items_param = op.get("items_param")
            if not items_param or items_param not in parent_inputs:
                orig0 = orig_children[0] if orig_children else children[0]
                items_param = param_bindings.get(orig0)
            if not items_param or items_param not in parent_inputs:
                return False, "map_elements needs list items_param", ""
            orig1 = orig_children[1] if len(orig_children) > 1 else children[1]
            child1 = children[1]
            child_prim = resolved.get(child1, child1)
            body_steps = []
            if orig1 in const_bindings:
                body_steps.append({
                    "id": "body0", "op": "constant",
                    "args": {"value": const_bindings[orig1]},
                })
            elif (child_prim == "identity"
                  or ((reuse_arg_bindings.get(orig1) or {}).get(
                      list((reuse_arg_bindings.get(orig1) or {})
                           .keys())[:1][0] if reuse_arg_bindings.get(orig1)
                      else None)
                      == "item" and child_prim == "identity")):
                id_prim = self.engine.primitives.get("identity")
                if id_prim is None:
                    return False, "identity primitive missing", ""
                id_ins = list(id_prim.inputs.keys())
                body_steps.append({
                    "id": "body0", "op": "identity",
                    "args": {id_ins[0]: {"$var": "item"}},
                })
            else:
                # Treat identity reuse explicitly
                bind = reuse_arg_bindings.get(orig1) or {}
                if child_prim == "identity" or (
                        resolved.get(child1) == "identity"):
                    id_prim = self.engine.primitives.get("identity")
                    id_ins = list(id_prim.inputs.keys())
                    body_steps.append({
                        "id": "body0", "op": "identity",
                        "args": {id_ins[0]: {"$var": "item"}},
                    })
                else:
                    if self.engine.primitives.get(child_prim) is None:
                        alt = None
                        for pname in sorted(self.engine.primitives.names()):
                            try:
                                prim_c = self.engine.primitives.get(pname)
                                if getattr(prim_c, "family", "") not in (
                                        "acquired", "promoted"):
                                    continue
                                pins = [
                                    k for k, v in prim_c.inputs.items()
                                    if not getattr(v, "optional", False)]
                                if len(pins) != 1:
                                    continue
                                ok = True
                                for args, expect in spec.examples:
                                    xs = args.get(items_param)
                                    if (not isinstance(xs, list)
                                            or not isinstance(expect, list)):
                                        ok = False
                                        break
                                    for x, y in zip(xs, expect):
                                        try:
                                            if prim_c.fn(
                                                    **{pins[0]: x}) != y:
                                                ok = False
                                                break
                                        except Exception:
                                            ok = False
                                            break
                                    if not ok:
                                        break
                                if ok:
                                    alt = pname
                                    break
                            except Exception:
                                continue
                        if alt is None:
                            return False, (
                                f"map element primitive {child_prim!r} "
                                f"not available"), ""
                        child_prim = alt
                    prim = self.engine.primitives.get(child_prim)
                    pins = [k for k, v in prim.inputs.items()
                            if not getattr(v, "optional", False)]
                    if len(pins) != 1:
                        return False, (
                            f"map element op {child_prim!r} must be unary"), ""
                    body_steps.append({
                        "id": "body0", "op": child_prim,
                        "args": {pins[0]: {"$var": "item"}},
                    })
            steps.append({
                "id": "varlen_map",
                "control": "foreach",
                "items": {"$param": items_param},
                "as": "item",
                "body": body_steps,
                "yield": {"$step": "body0"},
            })
            prev = "varlen_map"

        plan = {
            "name": node.name,
            "params": {nm: "any" for nm in parent_inputs},
            "steps": steps,
            "output": {"$step": prev},
        }
        analysis = self.engine.composer.analyze(plan)
        if not analysis.ok:
            errs = getattr(analysis, "errors", None) or []
            return False, (
                "assembled varlen sequence plan does not type-check: "
                + "; ".join(str(e) for e in list(errs)[:3])), ""
        from swarm_engine.acquisition.semantic import Case
        cases = [Case(args=dict(a), expect=v) for a, v in spec.examples]
        bad = []
        for case in cases:
            run = self.engine.composer.execute_sync(
                plan, dict(case.args), skip_check=True)
            if not run.get("success") or run.get("value") != case.expect:
                bad.append((case.args, run.get("value"), case.expect))
                if len(bad) >= 3:
                    break
        if bad:
            return False, (
                f"varlen sequence failed exact validation on parent "
                f"examples (e.g. got={bad[0][1]!r} expect={bad[0][2]!r})"), ""
        from swarm_engine.synthesis.admission import SmokeTest
        first_args, first_expect = spec.examples[0]
        try:
            verdict = self.engine.admission.admit(
                spec.description, plan,
                smoke=SmokeTest(args=dict(first_args), expect=first_expect))
        except Exception as exc:
            return False, f"varlen sequence admission error: {exc}", ""
        if not getattr(verdict, "ok", False):
            return False, (
                f"varlen sequence admission rejected: "
                f"{getattr(verdict, 'reason', getattr(verdict, 'stage', verdict))}"), ""
        self.engine.capabilities.bind_goal(
            spec.description, verdict.capability_id)
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        ok, detail = self._heldout_admission_gate(spec, verdict.capability_id)
        if not ok:
            self._reject_capability(verdict.capability_id, detail)
            return False, f"held-out admission rejected: {detail}", ""
        n = len(spec.examples)
        return True, (
            f"varlen sequence ({kind}) from {children}; "
            f"validated on {n}/{n} parent examples then admitted"), (
                verdict.capability_id)




    async def _assemble_varlen_set(self, node, spec, bdecomp, children):
        """Assemble varlen_set via as_set_of / map_to_set / range_set."""
        op = bdecomp.get("op") or {}
        if not isinstance(op, dict) or not op.get("op"):
            return False, f"varlen_set needs dict op, got {op!r}", ""
        kind = op.get("op")
        if kind not in ("as_set_of", "map_to_set", "range_set"):
            return False, f"unsupported varlen_set op {kind!r}", ""
        as_name = op.get("as_op") or (
            "as_frozenset" if op.get("set_kind") == "frozenset"
            else "as_set")
        if self.engine.primitives.get(as_name) is None:
            return False, f"{as_name} primitive not registered", ""
        as_ins = list(self.engine.primitives.get(as_name).inputs.keys())
        if len(as_ins) != 1:
            return False, f"{as_name} arity mismatch", ""
        if not spec.examples:
            return False, (
                "no parent examples; varlen_set cannot be validated"), ""
        resolved = bdecomp.get("resolved_primitives") or {}
        const_bindings = dict(bdecomp.get("const_bindings") or {})
        param_bindings = dict(bdecomp.get("param_bindings") or {})
        reuse_arg_bindings = dict(bdecomp.get("reuse_arg_bindings") or {})
        orig_children = list(bdecomp.get("children")
                             or bdecomp.get("child_refs") or [])
        parent_inputs = list(spec.input_names)

        def _as_arg(ref):
            if ref[0] == "const":
                return ref[1]
            if ref[0] == "param":
                return {"$param": ref[1]}
            if ref[0] == "step":
                return {"$step": ref[1]}
            return None

        def _child_ref(idx, peel_mode="identity"):
            child = children[idx]
            orig = orig_children[idx] if idx < len(orig_children) else child
            child_prim = resolved.get(child, child)
            steps_d = []
            if orig in const_bindings:
                return steps_d, ("const", const_bindings[orig])
            if child_prim == "identity" and orig in param_bindings:
                col = param_bindings[orig]
                if col not in parent_inputs:
                    return None, f"varlen_set param child needs {col!r}"
                id_ins = list(
                    self.engine.primitives.get("identity").inputs.keys())
                sid = f"vset_child_{idx}"
                steps_d.append({
                    "id": sid, "op": "identity",
                    "args": {id_ins[0]: {"$param": col}},
                })
                return steps_d, ("step", sid)
            if orig in param_bindings:
                col = param_bindings[orig]
                if col not in parent_inputs:
                    return None, f"varlen_set param bind needs {col!r}"
                return steps_d, ("param", col)
            if self.engine.primitives.get(child_prim) is None:
                peeled = []
                try:
                    for args, expect in spec.examples:
                        if peel_mode == "count":
                            peeled.append((dict(args), len(expect)))
                        else:
                            peeled.append((dict(args), expect))
                except Exception:
                    peeled = []
                alt = None
                if peeled and parent_inputs:
                    want = [o for _, o in peeled]
                    for pname in sorted(self.engine.primitives.names()):
                        try:
                            prim_c = self.engine.primitives.get(pname)
                            if getattr(prim_c, "family", "") not in (
                                    "acquired", "promoted"):
                                continue
                            pins = [
                                k for k, v in prim_c.inputs.items()
                                if not getattr(v, "optional", False)]
                            if len(pins) != 1:
                                continue
                            col = parent_inputs[0]
                            got, ok = [], True
                            for args, _e in peeled:
                                try:
                                    got.append(prim_c.fn(
                                        **{pins[0]: args[col]}))
                                except Exception:
                                    ok = False
                                    break
                            if ok and got == want:
                                alt = pname
                                break
                        except Exception:
                            continue
                if alt is None:
                    return None, (
                        f"varlen_set child primitive {child_prim!r} "
                        f"not available")
                child_prim = alt
            prim = self.engine.primitives.get(child_prim)
            pins = [k for k, v in prim.inputs.items()
                    if not getattr(v, "optional", False)]
            if len(pins) != 1:
                return None, f"varlen_set child {child_prim!r} not unary"
            bind = reuse_arg_bindings.get(orig) or {}
            src = (next(iter(bind.values())) if bind else parent_inputs[0])
            if src not in parent_inputs:
                src = parent_inputs[0]
            sid = f"vset_child_{idx}"
            steps_d.append({
                "id": sid, "op": child_prim,
                "args": {pins[0]: {"$param": src}},
            })
            return steps_d, ("step", sid)

        steps = []
        if kind == "as_set_of":
            if len(children) != 1:
                return False, "as_set_of needs exactly 1 child", ""
            items_param = op.get("items_param")
            if items_param and items_param in parent_inputs:
                list_arg = {"$param": items_param}
            else:
                sd, ref = _child_ref(0)
                if sd is None:
                    return False, ref, ""
                steps.extend(sd)
                list_arg = _as_arg(ref)
                if list_arg is None:
                    return False, f"as_set_of unresolved ref {ref!r}", ""
            steps.append({
                "id": "vset_as", "op": as_name,
                "args": {as_ins[0]: list_arg},
            })
            prev = "vset_as"
        elif kind == "range_set":
            if len(children) != 1:
                return False, "range_set needs exactly 1 child", ""
            if self.engine.primitives.get("range_list") is None:
                return False, "range_list primitive not registered", ""
            rl_ins = list(
                self.engine.primitives.get("range_list").inputs.keys())
            sd, ref = _child_ref(0, peel_mode="count")
            if sd is None:
                return False, ref, ""
            steps.extend(sd)
            carg = _as_arg(ref)
            if carg is None:
                return False, f"range_set unresolved count {ref!r}", ""
            steps.append({
                "id": "vset_range", "op": "range_list",
                "args": {rl_ins[0]: carg},
            })
            steps.append({
                "id": "vset_as", "op": as_name,
                "args": {as_ins[0]: {"$step": "vset_range"}},
            })
            prev = "vset_as"
        else:  # map_to_set
            if len(children) != 2:
                return False, "map_to_set needs exactly 2 children", ""
            items_param = op.get("items_param")
            if not items_param or items_param not in parent_inputs:
                return False, "map_to_set needs items_param", ""
            orig1 = (orig_children[1] if len(orig_children) > 1
                     else children[1])
            child1 = children[1]
            child_prim = resolved.get(child1, child1)
            body_steps = []
            if orig1 in const_bindings:
                body_steps.append({
                    "id": "body0", "op": "constant",
                    "args": {"value": const_bindings[orig1]},
                })
            elif (child_prim == "identity" or orig1 in param_bindings):
                id_ins = list(
                    self.engine.primitives.get("identity").inputs.keys())
                body_steps.append({
                    "id": "body0", "op": "identity",
                    "args": {id_ins[0]: {"$var": "item"}},
                })
            else:
                if self.engine.primitives.get(child_prim) is None:
                    return False, (
                        f"map_to_set element {child_prim!r} not available"
                    ), ""
                prim = self.engine.primitives.get(child_prim)
                pins = [k for k, v in prim.inputs.items()
                        if not getattr(v, "optional", False)]
                if len(pins) != 1:
                    return False, (
                        f"map_to_set element {child_prim!r} not unary"), ""
                body_steps.append({
                    "id": "body0", "op": child_prim,
                    "args": {pins[0]: {"$var": "item"}},
                })
            steps.append({
                "id": "vset_map",
                "control": "foreach",
                "items": {"$param": items_param},
                "as": "item",
                "body": body_steps,
                "yield": {"$step": "body0"},
            })
            steps.append({
                "id": "vset_as", "op": as_name,
                "args": {as_ins[0]: {"$step": "vset_map"}},
            })
            prev = "vset_as"

        plan = {
            "name": node.name,
            "params": {nm: "any" for nm in parent_inputs},
            "steps": steps,
            "output": {"$step": prev},
        }
        analysis = self.engine.composer.analyze(plan)
        if not analysis.ok:
            errs = getattr(analysis, "errors", None) or []
            return False, (
                "assembled varlen_set plan does not type-check: "
                + "; ".join(str(e) for e in list(errs)[:3])), ""
        from swarm_engine.acquisition.semantic import Case
        cases = [Case(args=dict(a), expect=v) for a, v in spec.examples]
        bad = []
        for case in cases:
            run = self.engine.composer.execute_sync(
                plan, dict(case.args), skip_check=True)
            if not run.get("success") or run.get("value") != case.expect:
                bad.append((case.args, run.get("value"), case.expect))
                if len(bad) >= 3:
                    break
        if bad:
            return False, (
                f"varlen_set failed exact validation "
                f"(e.g. got={bad[0][1]!r} expect={bad[0][2]!r})"), ""
        from swarm_engine.synthesis.admission import SmokeTest
        first_args, first_expect = spec.examples[0]
        try:
            verdict = self.engine.admission.admit(
                spec.description, plan,
                smoke=SmokeTest(args=dict(first_args), expect=first_expect))
        except Exception as exc:
            return False, f"varlen_set admission error: {exc}", ""
        if not getattr(verdict, "ok", False):
            return False, (
                f"varlen_set admission rejected: "
                f"{getattr(verdict, 'reason', getattr(verdict, 'stage', verdict))}"), ""
        self.engine.capabilities.bind_goal(
            spec.description, verdict.capability_id)
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        ok, detail = self._heldout_admission_gate(spec, verdict.capability_id)
        if not ok:
            self._reject_capability(verdict.capability_id, detail)
            return False, f"held-out admission rejected: {detail}", ""
        n = len(spec.examples)
        return True, (
            f"varlen_set ({kind}) from {children}; "
            f"validated on {n}/{n} parent examples then admitted"), (
                verdict.capability_id)

    async def _assemble_varlen_bytes(self, node, spec, bdecomp, children):
        """Assemble varlen_bytes via as_bytes_of / map_to_bytes /
        bytes_product / range_bytes."""
        op = bdecomp.get("op") or {}
        if not isinstance(op, dict) or not op.get("op"):
            return False, f"varlen_bytes needs dict op, got {op!r}", ""
        kind = op.get("op")
        if kind not in ("as_bytes_of", "map_to_bytes", "bytes_product",
                        "range_bytes"):
            return False, f"unsupported varlen_bytes op {kind!r}", ""
        as_name = op.get("as_op") or (
            "as_bytearray" if op.get("byte_kind") == "bytearray"
            else "as_bytes")
        if self.engine.primitives.get(as_name) is None:
            return False, f"{as_name} primitive not registered", ""
        as_ins = list(self.engine.primitives.get(as_name).inputs.keys())
        if len(as_ins) != 1:
            return False, f"{as_name} arity mismatch", ""
        if not spec.examples:
            return False, (
                "no parent examples; varlen_bytes cannot be validated"), ""
        resolved = bdecomp.get("resolved_primitives") or {}
        const_bindings = dict(bdecomp.get("const_bindings") or {})
        param_bindings = dict(bdecomp.get("param_bindings") or {})
        reuse_arg_bindings = dict(bdecomp.get("reuse_arg_bindings") or {})
        orig_children = list(bdecomp.get("children")
                             or bdecomp.get("child_refs") or [])
        parent_inputs = list(spec.input_names)

        def _as_arg(ref):
            if ref[0] == "const":
                return ref[1]
            if ref[0] == "param":
                return {"$param": ref[1]}
            if ref[0] == "step":
                return {"$step": ref[1]}
            return None

        def _child_ref(idx, peel_mode="ord"):
            child = children[idx]
            orig = orig_children[idx] if idx < len(orig_children) else child
            child_prim = resolved.get(child, child)
            steps_d = []
            if orig in const_bindings:
                return steps_d, ("const", const_bindings[orig])
            if child_prim == "identity" and orig in param_bindings:
                col = param_bindings[orig]
                if col not in parent_inputs:
                    return None, f"varlen_bytes param child needs {col!r}"
                id_ins = list(
                    self.engine.primitives.get("identity").inputs.keys())
                sid = f"vbytes_child_{idx}"
                steps_d.append({
                    "id": sid, "op": "identity",
                    "args": {id_ins[0]: {"$param": col}},
                })
                return steps_d, ("step", sid)
            if orig in param_bindings:
                col = param_bindings[orig]
                if col not in parent_inputs:
                    return None, f"varlen_bytes param bind needs {col!r}"
                return steps_d, ("param", col)
            if self.engine.primitives.get(child_prim) is None:
                peeled = []
                try:
                    for args, expect in spec.examples:
                        if peel_mode == "count":
                            peeled.append((dict(args), len(expect)))
                        elif peel_mode.startswith("slot"):
                            si = int(peel_mode.split(":")[1])
                            peeled.append((dict(args), list(expect)[si]))
                        else:
                            peeled.append((dict(args), list(expect)))
                except Exception:
                    peeled = []
                alt = None
                if peeled and parent_inputs:
                    want = [o for _, o in peeled]
                    for pname in sorted(self.engine.primitives.names()):
                        try:
                            prim_c = self.engine.primitives.get(pname)
                            if getattr(prim_c, "family", "") not in (
                                    "acquired", "promoted"):
                                continue
                            pins = [
                                k for k, v in prim_c.inputs.items()
                                if not getattr(v, "optional", False)]
                            if len(pins) != 1:
                                continue
                            col = parent_inputs[0]
                            got, ok = [], True
                            for args, _e in peeled:
                                try:
                                    got.append(prim_c.fn(
                                        **{pins[0]: args[col]}))
                                except Exception:
                                    ok = False
                                    break
                            if ok and got == want:
                                alt = pname
                                break
                        except Exception:
                            continue
                if alt is None:
                    return None, (
                        f"varlen_bytes child primitive {child_prim!r} "
                        f"not available")
                child_prim = alt
            prim = self.engine.primitives.get(child_prim)
            pins = [k for k, v in prim.inputs.items()
                    if not getattr(v, "optional", False)]
            if len(pins) != 1:
                return None, f"varlen_bytes child {child_prim!r} not unary"
            bind = reuse_arg_bindings.get(orig) or {}
            src = (next(iter(bind.values())) if bind else parent_inputs[0])
            if src not in parent_inputs:
                src = parent_inputs[0]
            sid = f"vbytes_child_{idx}"
            steps_d.append({
                "id": sid, "op": child_prim,
                "args": {pins[0]: {"$param": src}},
            })
            return steps_d, ("step", sid)

        steps = []
        if kind == "as_bytes_of":
            if len(children) != 1:
                return False, "as_bytes_of needs exactly 1 child", ""
            items_param = op.get("items_param")
            if items_param and items_param in parent_inputs:
                list_arg = {"$param": items_param}
            else:
                sd, ref = _child_ref(0)
                if sd is None:
                    return False, ref, ""
                steps.extend(sd)
                list_arg = _as_arg(ref)
            steps.append({
                "id": "vbytes_as", "op": as_name,
                "args": {as_ins[0]: list_arg},
            })
            prev = "vbytes_as"
        elif kind == "range_bytes":
            if len(children) != 1:
                return False, "range_bytes needs exactly 1 child", ""
            if self.engine.primitives.get("range_list") is None:
                return False, "range_list primitive not registered", ""
            rl_ins = list(
                self.engine.primitives.get("range_list").inputs.keys())
            sd, ref = _child_ref(0, peel_mode="count")
            if sd is None:
                return False, ref, ""
            steps.extend(sd)
            carg = _as_arg(ref)
            steps.append({
                "id": "vbytes_range", "op": "range_list",
                "args": {rl_ins[0]: carg},
            })
            steps.append({
                "id": "vbytes_as", "op": as_name,
                "args": {as_ins[0]: {"$step": "vbytes_range"}},
            })
            prev = "vbytes_as"
        elif kind == "map_to_bytes":
            if len(children) != 2:
                return False, "map_to_bytes needs exactly 2 children", ""
            items_param = op.get("items_param")
            if not items_param or items_param not in parent_inputs:
                return False, "map_to_bytes needs items_param", ""
            orig1 = (orig_children[1] if len(orig_children) > 1
                     else children[1])
            child1 = children[1]
            child_prim = resolved.get(child1, child1)
            body_steps = []
            if orig1 in const_bindings:
                body_steps.append({
                    "id": "body0", "op": "constant",
                    "args": {"value": const_bindings[orig1]},
                })
            elif child_prim == "identity" or orig1 in param_bindings:
                id_ins = list(
                    self.engine.primitives.get("identity").inputs.keys())
                body_steps.append({
                    "id": "body0", "op": "identity",
                    "args": {id_ins[0]: {"$var": "item"}},
                })
            else:
                if self.engine.primitives.get(child_prim) is None:
                    return False, (
                        f"map_to_bytes element {child_prim!r} missing"), ""
                prim = self.engine.primitives.get(child_prim)
                pins = [k for k, v in prim.inputs.items()
                        if not getattr(v, "optional", False)]
                if len(pins) != 1:
                    return False, f"map element {child_prim!r} not unary", ""
                body_steps.append({
                    "id": "body0", "op": child_prim,
                    "args": {pins[0]: {"$var": "item"}},
                })
            steps.append({
                "id": "vbytes_map",
                "control": "foreach",
                "items": {"$param": items_param},
                "as": "item",
                "body": body_steps,
                "yield": {"$step": "body0"},
            })
            steps.append({
                "id": "vbytes_as", "op": as_name,
                "args": {as_ins[0]: {"$step": "vbytes_map"}},
            })
            prev = "vbytes_as"
        else:  # bytes_product
            k = len(children)
            if not (2 <= k <= 7):
                return False, (
                    f"bytes_product needs 2..7 children (got {k})"), ""
            if self.engine.primitives.get("lift") is None:
                return False, "lift primitive not registered", ""
            if self.engine.primitives.get("append") is None:
                return False, "append primitive not registered", ""
            lift_ins = list(self.engine.primitives.get("lift").inputs.keys())
            app_ins = list(
                self.engine.primitives.get("append").inputs.keys())
            slot_refs = []
            for i in range(k):
                sd, ref = _child_ref(i, peel_mode=f"slot:{i}")
                if sd is None:
                    return False, ref, ""
                steps.extend(sd)
                slot_refs.append(ref)
            steps.append({
                "id": "pack0", "op": "lift",
                "args": {lift_ins[0]: _as_arg(slot_refs[0])},
            })
            prev_pack = "pack0"
            for j in range(1, k):
                sid = f"pack{j}"
                steps.append({
                    "id": sid, "op": "append",
                    "args": {
                        app_ins[0]: {"$step": prev_pack},
                        app_ins[1]: _as_arg(slot_refs[j]),
                    },
                })
                prev_pack = sid
            steps.append({
                "id": "vbytes_as", "op": as_name,
                "args": {as_ins[0]: {"$step": prev_pack}},
            })
            prev = "vbytes_as"

        plan = {
            "name": node.name,
            "params": {nm: "any" for nm in parent_inputs},
            "steps": steps,
            "output": {"$step": prev},
        }
        analysis = self.engine.composer.analyze(plan)
        if not analysis.ok:
            errs = getattr(analysis, "errors", None) or []
            return False, (
                "assembled varlen_bytes plan does not type-check: "
                + "; ".join(str(e) for e in list(errs)[:3])), ""
        from swarm_engine.acquisition.semantic import Case
        cases = [Case(args=dict(a), expect=v) for a, v in spec.examples]
        bad = []
        for case in cases:
            run = self.engine.composer.execute_sync(
                plan, dict(case.args), skip_check=True)
            if not run.get("success") or run.get("value") != case.expect:
                bad.append((case.args, run.get("value"), case.expect))
                if len(bad) >= 3:
                    break
        if bad:
            return False, (
                f"varlen_bytes failed exact validation "
                f"(e.g. got={bad[0][1]!r} expect={bad[0][2]!r})"), ""
        from swarm_engine.synthesis.admission import SmokeTest
        first_args, first_expect = spec.examples[0]
        try:
            verdict = self.engine.admission.admit(
                spec.description, plan,
                smoke=SmokeTest(args=dict(first_args), expect=first_expect))
        except Exception as exc:
            return False, f"varlen_bytes admission error: {exc}", ""
        if not getattr(verdict, "ok", False):
            return False, (
                f"varlen_bytes admission rejected: "
                f"{getattr(verdict, 'reason', getattr(verdict, 'stage', verdict))}"), ""
        self.engine.capabilities.bind_goal(
            spec.description, verdict.capability_id)
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        ok, detail = self._heldout_admission_gate(spec, verdict.capability_id)
        if not ok:
            self._reject_capability(verdict.capability_id, detail)
            return False, f"held-out admission rejected: {detail}", ""
        n = len(spec.examples)
        return True, (
            f"varlen_bytes ({kind}) from {children}; "
            f"validated on {n}/{n} parent examples then admitted"), (
                verdict.capability_id)


    async def _assemble_set_algebra(self, node, spec, bdecomp, children):
        """Assemble set_algebra as a binary set op over two param children."""
        op = bdecomp.get("op") or {}
        if not isinstance(op, dict) or not op.get("op"):
            return False, f"set_algebra needs dict op, got {op!r}", ""
        kind = op.get("op")
        if kind not in ("set_union", "set_intersect", "set_difference",
                        "frozenset_union"):
            return False, f"unsupported set_algebra op {kind!r}", ""
        if self.engine.primitives.get(kind) is None:
            return False, f"{kind} primitive not registered", ""
        pins = list(self.engine.primitives.get(kind).inputs.keys())
        if len(pins) != 2:
            return False, f"{kind} arity mismatch", ""
        if len(children) != 2:
            return False, "set_algebra needs exactly 2 children", ""
        if not spec.examples:
            return False, "no parent examples; set_algebra cannot validate", ""
        left = op.get("left_param")
        right = op.get("right_param")
        parent_inputs = list(spec.input_names)
        if not left or left not in parent_inputs:
            return False, f"set_algebra needs left_param, got {left!r}", ""
        if not right or right not in parent_inputs:
            return False, f"set_algebra needs right_param, got {right!r}", ""
        steps = [{
            "id": "salg",
            "op": kind,
            "args": {
                pins[0]: {"$param": left},
                pins[1]: {"$param": right},
            },
        }]
        plan = {
            "name": node.name,
            "params": {nm: "any" for nm in parent_inputs},
            "steps": steps,
            "output": {"$step": "salg"},
        }
        analysis = self.engine.composer.analyze(plan)
        if not analysis.ok:
            errs = getattr(analysis, "errors", None) or []
            return False, (
                "assembled set_algebra plan does not type-check: "
                + "; ".join(str(e) for e in list(errs)[:3])), ""
        from swarm_engine.acquisition.semantic import Case
        cases = [Case(args=dict(a), expect=v) for a, v in spec.examples]
        bad = []
        for case in cases:
            run = self.engine.composer.execute_sync(
                plan, dict(case.args), skip_check=True)
            if not run.get("success") or run.get("value") != case.expect:
                bad.append((case.args, run.get("value"), case.expect))
                if len(bad) >= 3:
                    break
        if bad:
            return False, (
                f"set_algebra failed exact validation "
                f"(e.g. got={bad[0][1]!r} expect={bad[0][2]!r})"), ""
        from swarm_engine.synthesis.admission import SmokeTest
        first_args, first_expect = spec.examples[0]
        try:
            verdict = self.engine.admission.admit(
                spec.description, plan,
                smoke=SmokeTest(args=dict(first_args), expect=first_expect))
        except Exception as exc:
            return False, f"set_algebra admission error: {exc}", ""
        if not getattr(verdict, "ok", False):
            return False, (
                f"set_algebra admission rejected: "
                f"{getattr(verdict, 'reason', getattr(verdict, 'stage', verdict))}"), ""
        self.engine.capabilities.bind_goal(
            spec.description, verdict.capability_id)
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        ok, detail = self._heldout_admission_gate(spec, verdict.capability_id)
        if not ok:
            self._reject_capability(verdict.capability_id, detail)
            return False, f"held-out admission rejected: {detail}", ""
        n = len(spec.examples)
        return True, (
            f"set_algebra ({kind}) from {children}; "
            f"validated on {n}/{n} parent examples then admitted"), (
                verdict.capability_id)

    async def _assemble_behavioral_parent(self, node, spec,
                                          bdecomp: Dict[str, Any]
                                          ) -> Tuple[bool, str, str]:
        """Assemble a behavioral-decomposition (P8) parent from its children.

        Mirrors `_try_pair_assembly`: the plan is constructed from the
        requirement's own behavioral metadata -- family, op, child_refs --
        never from goal text, and is behaviorally validated on ALL of the
        parent's examples before admission. `associative_fold` left-folds
        the child calls with the contract's binary op; `nest_chain` folds
        the children (the v1 inner fold is always additive) then chains the
        outer ops (unary or binary-with-literal) innermost-first. The fold/nest operators are
        re-discovered here from the registry (I2 re-checked: the registry
        may have changed since analysis); the contract only constrained
        which family/op/children the assembly must use.
        """
        from typing import List as _List
        family = bdecomp.get("family")
        op = bdecomp.get("op")
        children = list(bdecomp.get("children")
                        or bdecomp.get("child_refs") or [])
        if family not in ("associative_fold", "nest_chain",
                          "structural_wrap", "structural_product",
                          "varlen_sequence", "varlen_set", "varlen_bytes",
                          "set_algebra"):
            return False, f"unknown behavioral family {family!r}", ""
        if not children:
            return False, "behavioral_decomposition names no children", ""
        if family == "structural_wrap":
            return await self._assemble_structural_wrap(
                node, spec, bdecomp, children)
        if family == "structural_product":
            return await self._assemble_structural_product(
                node, spec, bdecomp, children)
        if family == "varlen_sequence":
            return await self._assemble_varlen_sequence(
                node, spec, bdecomp, children)
        if family == "varlen_set":
            return await self._assemble_varlen_set(
                node, spec, bdecomp, children)
        if family == "varlen_bytes":
            return await self._assemble_varlen_bytes(
                node, spec, bdecomp, children)
        if family == "set_algebra":
            return await self._assemble_set_algebra(
                node, spec, bdecomp, children)
        # Gap-A: remap through the parent's behavioral metadata. A child
        # satisfied by behavioral discovery executes under its DISCOVERED
        # primitive name, not this decomposition's node name. Children
        # absent from the map keep their node names: freshly acquired
        # children register under exactly those. Fail-closed below on any
        # name that is not a registered primitive.
        resolved = bdecomp.get("resolved_primitives") or {}
        orig_children_gate = list(children)
        if resolved:
            children = [resolved.get(c, c) for c in children]
        if not spec.examples:
            return False, ("no parent examples; the assembled behavioral "
                           "composition cannot be validated"), ""
        const_bindings_gate = dict(bdecomp.get("const_bindings") or {})
        kv_bindings_gate = dict(bdecomp.get("kv_bindings") or {})
        missing = []
        for i, c in enumerate(children):
            orig = orig_children_gate[i]
            if (orig in const_bindings_gate or c == "__const__"
                    or orig in kv_bindings_gate or c == "__kv__"):
                continue
            if self.engine.primitives.get(c) is None:
                missing.append(c)
        if missing:
            return False, (f"prerequisite(s) {missing} are not available "
                           f"primitives yet; the parent cannot be assembled "
                           f"before its parts"), ""
        # Re-resolve the operators from the live registry (I2 at assembly
        # time). v1: the fold op is the contract's binary op name; a nest's
        # inner fold is always additive and its outer ops are unary names
        # innermost-first.
        if family == "associative_fold":
            fold_op = op if isinstance(op, str) else None
            outer_ops: _List[str] = []
        else:
            fold_op = "add"
            outer_ops = list(op) if isinstance(op, list) else [op]
        if not fold_op or self.engine.primitives.get(fold_op) is None:
            return False, (f"fold op {fold_op!r} is not a registered "
                           f"primitive"), ""
        fold_ins = list(self.engine.primitives.get(fold_op).inputs.keys())
        if len(fold_ins) != 2:
            return False, f"fold op {fold_op!r} is not binary", ""
        # Outer specs: ("unary", name, in_name) or
        # ("binlit", name, pins, free_index, bound).
        outer_prims = []
        for ospec in outer_ops:
            if isinstance(ospec, str):
                p = self.engine.primitives.get(ospec)
                if p is None:
                    return False, (f"outer op {ospec!r} is not a registered "
                                   f"primitive"), ""
                pins = [k for k, v in p.inputs.items() if not v.optional]
                if len(pins) != 1:
                    return False, f"outer op {ospec!r} is not unary", ""
                outer_prims.append(("unary", ospec, pins[0]))
                continue
            if not isinstance(ospec, dict):
                return False, f"outer op spec {ospec!r} is not supported", ""
            pname = ospec.get("op")
            free_index = ospec.get("free_index")
            bound = dict(ospec.get("bound") or {})
            p = self.engine.primitives.get(pname) if pname else None
            if p is None:
                return False, (f"outer op {pname!r} is not a registered "
                               f"primitive"), ""
            pins = [k for k, v in p.inputs.items() if not v.optional]
            if (len(pins) != 2
                    or not isinstance(free_index, int)
                    or free_index not in (0, 1)
                    or pins[1 - free_index] not in bound):
                return False, (
                    f"outer binary-with-literal spec {ospec!r} is malformed"), ""
            outer_prims.append(("binlit", pname, pins, free_index, bound))

        parent_inputs = list(spec.input_names)
        # Param-exact children (v21): resolved name is "identity" but the
        # binding column is in param_bindings keyed by the ORIGINAL child
        # node name (before Gap-A remap).
        param_bindings = dict(bdecomp.get("param_bindings") or {})
        reuse_arg_bindings = dict(bdecomp.get("reuse_arg_bindings") or {})
        const_bindings = dict(bdecomp.get("const_bindings") or {})
        kv_bindings = dict(bdecomp.get("kv_bindings") or {})
        orig_children = list(bdecomp.get("children")
                             or bdecomp.get("child_refs") or [])
        steps = []
        # part_refs[i] is either a step id ("part{i}") or a bare literal
        # for behavioral_const children (inlined into the fold args).
        part_refs = []
        for i, child in enumerate(children):
            orig = orig_children[i] if i < len(orig_children) else child
            if orig in const_bindings:
                part_refs.append(("const", const_bindings[orig]))
                continue
            if orig in kv_bindings:
                # Singleton dict wrap: kv(key, value) where value is a
                # parent param or a reused unary on a parent column.
                meta = kv_bindings[orig] or {}
                bind = dict(meta.get("bind") or {})
                key = bind.get("__kv_key__")
                mode = bind.get("__kv_mode__")
                value_ref = meta.get("value_ref")
                if key is None or mode not in ("param", "reuse") or not value_ref:
                    return False, f"malformed kv_bindings for {orig!r}", ""
                if self.engine.primitives.get("kv") is None:
                    return False, "kv primitive is not registered", ""
                kid = f"kv{i}"
                if mode == "param":
                    if value_ref not in parent_inputs:
                        return False, (
                            f"kv param mode needs parent input "
                            f"{value_ref!r}"), ""
                    steps.append({
                        "id": kid, "op": "kv",
                        "args": {
                            "key": key,
                            "value": {"$param": value_ref},
                        },
                    })
                else:
                    # reuse: call value_ref unary on __kv_col__
                    col = bind.get("__kv_col__")
                    if col not in parent_inputs:
                        return False, (
                            f"kv reuse mode needs parent input "
                            f"{col!r}"), ""
                    if self.engine.primitives.get(value_ref) is None:
                        return False, (
                            f"kv value capability {value_ref!r} missing"), ""
                    vin = list(self.engine.primitives.get(value_ref).inputs.keys())
                    if len(vin) != 1:
                        return False, (
                            f"kv value capability {value_ref!r} not unary"), ""
                    inner = f"kvinner{i}"
                    steps.append({
                        "id": inner, "op": value_ref,
                        "args": {vin[0]: {"$param": col}},
                    })
                    steps.append({
                        "id": kid, "op": "kv",
                        "args": {
                            "key": key,
                            "value": {"$step": inner},
                        },
                    })
                part_refs.append(("step", kid))
                continue
            bind_col = param_bindings.get(orig)
            prim = self.engine.primitives.get(child)
            if bind_col is not None and child == "identity":
                if bind_col not in parent_inputs:
                    return False, (f"param-exact child needs parent input "
                                   f"{bind_col!r} which is absent"), ""
                id_ins = list(prim.inputs.keys())
                if len(id_ins) != 1:
                    return False, "identity primitive is not unary", ""
                steps.append({"id": f"part{i}", "op": "identity",
                              "args": {id_ins[0]: {"$param": bind_col}}})
                part_refs.append(("step", f"part{i}"))
                continue
            arg_bind = reuse_arg_bindings.get(orig)
            if arg_bind:
                # Cross-name reuse + partial-app literal binds.
                # Keys "__lit__:<prim_input>" carry a bound literal from
                # the shared vocab (partial application); other keys remap
                # prim inputs to parent params.
                #
                # v30: when literal binds are present, materialize a
                # first-class residual-arity capability (persist the
                # fewer-arity view), then wire the parent to that residual
                # so later tasks can reuse it without re-binding. Falls
                # back to inline lit binds if materialization fails.
                lit_present = any(
                    isinstance(pin, str) and pin.startswith("__lit__:")
                    for pin in arg_bind)
                residual = None
                if lit_present:
                    residual = self._materialize_partial_residual(
                        child, arg_bind, parent_inputs)
                if residual is not None:
                    rname, free_args = residual
                    steps.append({
                        "id": f"part{i}", "op": rname,
                        "args": free_args,
                    })
                    part_refs.append(("step", f"part{i}"))
                    continue
                args = {}
                missing = []
                for pin, val in arg_bind.items():
                    if pin == "__wrap__":
                        continue
                    if isinstance(pin, str) and pin.startswith("__lit__:"):
                        real = pin.split(":", 1)[1]
                        lit = val
                        if isinstance(lit, str):
                            try:
                                lit = int(lit)
                            except ValueError:
                                try:
                                    lit = float(lit)
                                except ValueError:
                                    pass
                        args[real] = lit
                    else:
                        if val not in parent_inputs:
                            missing.append(val)
                        else:
                            args[pin] = {"$param": val}
                if missing:
                    return False, (f"reuse arg bind for {child!r} needs "
                                   f"parent input(s) {missing}"), ""
                # v38: cross-rep serialize wrap around a reused numeric
                # unary — emit inner call then serialize(obj=inner).
                wrap = None
                if "__wrap__" in arg_bind:
                    wrap = arg_bind.get("__wrap__")
                    args.pop("__wrap__", None)
                if wrap == "serialize":
                    if self.engine.primitives.get("serialize") is None:
                        return False, "serialize primitive missing for wrap", ""
                    # Drop wrap key from arg iteration leftovers
                    args = {k: v for k, v in args.items()
                            if k != "__wrap__"}
                    inner_id = f"part{i}_inner"
                    steps.append({
                        "id": inner_id, "op": child,
                        "args": args,
                    })
                    steps.append({
                        "id": f"part{i}", "op": "serialize",
                        "args": {"obj": {"$step": inner_id}},
                    })
                    part_refs.append(("step", f"part{i}"))
                    continue
                steps.append({
                    "id": f"part{i}", "op": child,
                    "args": args,
                })
                part_refs.append(("step", f"part{i}"))
                continue
            child_inputs = list(prim.inputs.keys())
            unmapped = [n for n in child_inputs if n not in parent_inputs]
            if unmapped:
                return False, (f"prerequisite {child!r} needs input(s) "
                               f"{unmapped} the parent does not provide; "
                               f"refusing rather than inventing values"), ""
            steps.append({"id": f"part{i}", "op": child,
                          "args": {n: {"$param": n} for n in child_inputs}})
            part_refs.append(("step", f"part{i}"))
        if len(part_refs) != len(children):
            return False, "internal: part_refs/children length mismatch", ""
        # Left fold of the child calls with the contract's binary op.
        # Const children contribute bare literals rather than $step refs.
        def _ref(kind_val):
            kind, val = kind_val
            if kind == "const":
                return val
            return {"$step": val}

        kind0, val0 = part_refs[0]
        if kind0 == "const":
            # Degenerate: leading const — seed with a no-op identity of
            # the const via a tiny literal-bearing fold against the next
            # part. For k>=2 folds the first part is rarely const; if it
            # is, emit a trivial identity-of-const using serialize/passthrough
            # is unnecessary: use the const as the running value directly.
            prev_ref = val0
            prev_is_const = True
        else:
            prev_ref = val0
            prev_is_const = False
        for i in range(1, len(children)):
            fid = f"fold{i-1}"
            left = prev_ref if prev_is_const else {"$step": prev_ref}
            right = _ref(part_refs[i])
            steps.append({"id": fid, "op": fold_op,
                          "args": {fold_ins[0]: left,
                                   fold_ins[1]: right}})
            prev_ref = fid
            prev_is_const = False
        prev = prev_ref
        # Nest: chain outer ops innermost-first (unary or binary-with-lit).
        for j, ospec in enumerate(outer_prims):
            uid = f"outer{j}"
            if ospec[0] == "unary":
                _, uname, in_name = ospec
                steps.append({"id": uid, "op": uname,
                              "args": {in_name: {"$step": prev}}})
            else:
                _, pname, pins, free_index, bound = ospec
                args = {}
                for i, pin in enumerate(pins):
                    if i == free_index:
                        args[pin] = {"$step": prev}
                    else:
                        args[pin] = bound[pin]
                steps.append({"id": uid, "op": pname, "args": args})
            prev = uid
        plan = {"name": node.name,
                "params": {n: "any" for n in parent_inputs},
                "steps": steps, "output": {"$step": prev}}

        if not self.engine.composer.analyze(plan).ok:
            return False, "assembled behavioral plan does not type-check", ""
        # Exact behavioral validation on ALL parent examples -- the same
        # honesty as pair-assembly's all-examples check.
        from swarm_engine.acquisition.semantic import Case
        cases = [Case(args=dict(a), expect=v) for a, v in spec.examples]
        bad = []
        for case in cases:
            run = self.engine.composer.execute_sync(
                plan, dict(case.args), skip_check=True)
            passed, why = case.judge(
                bool(run.get("success")), run.get("value"),
                str(run.get("error") or ""))
            if not passed:
                bad.append((case.args, run.get("value"), case.expect, why))
                break
        if bad:
            args, got, want, why = bad[0]
            return False, (f"assembled behavioral composition failed "
                           f"validation on the parent's examples: {args} -> "
                           f"{got!r} ({why}; expected {want!r})"), ""
        # No semantic-template guard here: the assembly was constructed from
        # the requirement's own behavioral metadata and validated exactly on
        # every parent example. A coincidental skeleton cannot reproduce
        # exact outputs; the examples are the evidence.
        from swarm_engine.synthesis.admission import SmokeTest
        first_args, first_expect = spec.examples[0]
        verdict = self.engine.admission.admit(
            spec.description, plan,
            smoke=SmokeTest(args=dict(first_args), expect=first_expect))
        if not verdict.ok:
            return False, f"admission refused: {verdict.stage}", ""
        self.engine.capabilities.bind_goal(spec.description, verdict.capability_id)
        # A behaviorally-assembled parent references its children via
        # acquired.* primitives, so it is not portable standalone; register
        # it explicitly for grandparent addressability (mirrors
        # _try_pair_assembly's multi-level comment).
        try:
            rec = self.engine.capabilities.get(verdict.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    verdict.capability_id, rec)
        except Exception:
            pass
        # Held-out gate (design 2.8 step 8): the train/holdout split +
        # synthetic unseen-input check. A parent that only fits its
        # training examples is rejected here, not admitted.
        ok, detail = self._heldout_admission_gate(spec, verdict.capability_id)
        if not ok:
            self._reject_capability(verdict.capability_id, detail)
            return False, f"held-out admission rejected: {detail}", ""
        n = len(spec.examples)
        return True, (f"behaviorally assembled ({family}) from {children}; "
                      f"validated on {n}/{n} parent examples then admitted; "
                      f"{detail}"), verdict.capability_id

    def _bind_synthesis_oracle(self, oracle):
        """O9 channel binding for the ambiguity-resolution oracle.

        Returns the oracle wrapped for per-query verification + recording
        (see BoundCallable), or None when no oracle is installed. Raises
        OracleBindingError when an oracle IS installed but was not put in
        place via engine.bind_synthesis_oracle -- i.e. it is unregistered
        and must not steer synthesis. Without an engine oracle registry,
        the legacy behavior is preserved exactly (unbound).
        """
        if oracle is None:
            return None
        reg = getattr(self.engine, "oracle_registry", None)
        handle = getattr(self.engine, "oracle", None)
        if reg is None or handle is None:
            return oracle  # unbound configuration: legacy behavior
        from swarm_engine.governance.binding_helpers import (
            BoundCallable, verify_live_callable)
        if isinstance(oracle, BoundCallable):
            return oracle
        binding = getattr(self.engine, "_synthesis_oracle_binding", None)
        if (not isinstance(binding, dict) or binding.get("fn") is not oracle
                or "oracle_id" not in binding):
            from swarm_engine.governance.oracle_binding import (
                OracleBindingError)
            raise OracleBindingError(
                "refusing to resolve synthesis ambiguity with an "
                "unregistered oracle: engine.synthesis_oracle was not "
                "installed via engine.bind_synthesis_oracle(fn, producer_id, "
                "token)")
        # Re-verify the registered definition still matches the live callable
        # before it steers anything (the O1 re-digest pattern).
        verify_live_callable(reg, binding["oracle_id"], binding["version"],
                             oracle, what="synthesis_oracle")
        return BoundCallable(reg, handle, oracle, binding["oracle_id"],
                             binding["version"], binding["producer_id"],
                             what="synthesis_oracle")

    async def _try_generate(self, node, spec, delegate) -> Tuple[bool, str, str]:
        if not spec.examples:
            return False, "no examples; generation cannot be validated", ""
        # O9 refusal note; set when the cognition fallback runs below.
        _oracle_refused = ""
        outcome = await self.engine.acquire_capability(
            node.name, spec.description, spec.examples,
            required_effects=spec.required_effects)
        if outcome.get("acquired"):
            cap_id = outcome["capability_id"]
            ok, detail = self._heldout_admission_gate(spec, cap_id)
            if not ok:
                self._reject_capability(cap_id, detail)
                return False, f"held-out admission rejected: {detail}", ""
            return True, f"synthesized, independently validated, {detail}", cap_id
        # Generic fallback: the source-code-generation path above cannot
        # represent every behavioral class (e.g. threshold/piecewise
        # conditionals). GeneralSynthesizer, already proven this session
        # to reach exactly this class via the symbolic-synthesis bridge,
        # is tried here as a second, real attempt within the SAME
        # "generate" strategy slot -- not a new StrategySelector-dispatched
        # strategy, and not a per-domain branch: every "generate" attempt
        # that reaches this point tries it generically.
        cognition = getattr(self.engine, "cognition", None)
        if cognition is not None and spec.input_names:
            try:
                oracle = getattr(self.engine, "synthesis_oracle", None)
                # O9: an ambiguity-resolution oracle steers which hypothesis
                # wins -- it may only do so as a REGISTERED oracle. An
                # unregistered oracle is refused outright; a registered one
                # is wrapped so every query is re-verified against its
                # registered definition and recorded as a chained
                # (args-digest -> value) evaluation.
                oracle = self._bind_synthesis_oracle(oracle)
            except Exception as exc:
                # Refused (or unavailable): the oracle must not steer
                # synthesis, so continue WITHOUT it. The refusal is carried
                # in the returned detail, not swallowed.
                from swarm_engine.governance.oracle_binding import (
                    OracleBindingError)
                if isinstance(exc, OracleBindingError):
                    _oracle_refused = f"synthesis oracle refused: {exc}; "
                oracle = None
            # When an oracle is available for ambiguity resolution,
            # give synthesis a larger generic budget so multi-branch
            # conditionals can form before the combinatorial wall.
            reasoning = getattr(cognition, "reasoning", None)
            syn = getattr(reasoning, "synthesizer", None) if reasoning else None
            _saved = None
            if oracle is not None and syn is not None:
                _saved = (syn.max_size, syn.max_candidates,
                          getattr(syn, "wall_clock_limit_s", 20.0))
                syn.max_size = max(syn.max_size, 6)
                syn.max_candidates = max(syn.max_candidates, 80000)
                syn.wall_clock_limit_s = max(
                    getattr(syn, "wall_clock_limit_s", 20.0), 35.0)
            try:
                result = cognition.propose_multi(
                    spec.description, spec.examples,
                    tuple(spec.input_names), oracle=oracle)
            except Exception:
                result = None
            finally:
                if _saved is not None and syn is not None:
                    syn.max_size, syn.max_candidates, syn.wall_clock_limit_s = _saved
            if result is not None and result.solved and result.plan:
                from swarm_engine.synthesis.admission import SmokeTest
                first_args, first_expect = spec.examples[0]
                smoke = SmokeTest(args=dict(first_args), expect=first_expect)
                verdict = self.engine.admission.admit(spec.description, result.plan, smoke=smoke)
                if verdict.ok:
                    ok, detail = self._heldout_admission_gate(spec, verdict.capability_id)
                    if not ok:
                        self._reject_capability(verdict.capability_id, detail)
                        return False, f"held-out admission rejected: {detail}", ""
                    # Bind canonical goal/node name for fresh-process lookup
                    try:
                        self.engine.capabilities.bind_goal(
                            node.name, verdict.capability_id)
                    except Exception:
                        pass
                    try:
                        prim_name = f"acquired.{verdict.capability_id}"
                        src = self.engine.primitives.get(prim_name)
                        if src is not None and self.engine.primitives.get(node.name) is None:
                            from swarm_engine.primitives.core import Primitive
                            self.engine.primitives.register(Primitive(
                                name=node.name,
                                family=getattr(src, "family", "acquired"),
                                fn=src.fn,
                                inputs=dict(getattr(src, "inputs", {}) or {}),
                                output=getattr(src, "output", None),
                                effects=tuple(getattr(src, "effects", ()) or ()),
                                doc=f"alias of {prim_name} for node {node.name}",
                            ), overwrite=True)
                            # Track the alias so revocation unregisters it
                            # (2026-09-19 R11).
                            try:
                                ti = getattr(self.engine.primitives,
                                             "_acquired_capability_ids", None)
                                if isinstance(ti, dict):
                                    ti[node.name] = verdict.capability_id
                            except Exception:
                                pass
                    except Exception:
                        pass
                    return (True,
                            f"{_oracle_refused}synthesized via symbolic search "
                            f"({result.candidates_tried} candidates), "
                            f"independently admitted, {detail}",
                            verdict.capability_id)
        return False, _oracle_refused + outcome.get("reason", "generation failed"), ""

    def _ensure_plan_op_primitives(self) -> None:
        """Register plan-op primitives that stored plans may reference.

        2026-09-22 (F3 R12): `discrete_lookup` and `constant` are registered
        lazily by the experiment route, but admitted plans persist with those
        ops. A fresh boot audits stored plans against the live registry
        BEFORE any experiment runs, so a healthy discrete_lookup/constant
        capability was quarantined on reboot for a dependency that is
        restorable in this same process. Ensuring them here (called from
        boot before the audit) keeps the audit honest: it still quarantines
        capabilities whose ops are genuinely gone.
        """
        try:
            from swarm_engine.primitives.core import Primitive, Effect, ANY
            if self.engine.primitives.get("discrete_lookup") is None:
                from swarm_engine.primitives.discrete_lookup import (
                    discrete_lookup as _dl)
                self.engine.primitives.register(Primitive(
                    name="discrete_lookup", family="builtin", fn=_dl,
                    inputs={"table": ANY, "key": ANY}, output=ANY,
                    effects=(Effect.PURE,)), overwrite=True)
            if self.engine.primitives.get("constant") is None:
                self.engine.primitives.register(Primitive(
                    name="constant", family="builtin",
                    fn=lambda value: value,
                    inputs={"value": ANY}, output=ANY,
                    effects=(Effect.PURE,)), overwrite=True)
        except Exception:
            pass

    async def _try_experiment(self, node, spec, delegate) -> Tuple[bool, str, str]:
        """Induce a discrete finite map from examples and admit via
        AdmissionController as discrete_lookup.

        IndependentValidator rejects pure tables as non-generalising (by
        design). For projected leaf examples of a structural decomposition,
        a finite map *is* the correct capability: held-out keys remain None
        or raise only if the table lacks them. Route through the same
        discrete_lookup admission path already proven for relational tables
        rather than the code-candidate IndependentValidator gauntlet.
        """
        if not spec.examples:
            return False, "no examples to induce from", ""
        input_names = list(spec.input_names or sorted(spec.examples[0][0].keys()))
        if len(input_names) != 1:
            return False, "experiment discrete_lookup path requires a single input name", ""
        key = input_names[0]
        table = {}
        for args, value in spec.examples:
            k = dict(args)[key]
            try:
                hash(k)
            except TypeError:
                return False, (
                    "experiment discrete_lookup path requires hashable "
                    "input keys (e.g. not list/dict)"), ""
            table[k] = value
        if not table:
            return False, "induction produced empty table", ""
        # Constant-output class: when every example yields the same value,
        # admit a constant plan (not discrete_lookup). Lookup returns null
        # on unseen keys and fails the synthetic held-out gate; a true
        # constant generalizes. Generic — no task-specific empty-list hack.
        _vals = list(table.values())
        if _vals and all(v == _vals[0] for v in _vals):
            const_v = _vals[0]
            if self.engine.primitives.get("constant") is None:
                from swarm_engine.primitives.core import Primitive, Effect, ANY
                self.engine.primitives.register(Primitive(
                    name="constant", family="builtin",
                    fn=lambda value: value,
                    inputs={"value": ANY}, output=ANY,
                    effects=(Effect.PURE,)), overwrite=True)
            plan = {
                "params": {key: "any"},
                "steps": [{"id": "c0", "op": "constant",
                           "args": {"value": const_v}}],
                "output": {"$step": "c0"},
            }
            from swarm_engine.synthesis.admission import SmokeTest
            first_args, first_expect = spec.examples[0]
            for args, expect in spec.examples:
                run = self.engine.composer.execute_sync(plan, dict(args))
                if not (run.get("success") and run.get("value") == expect):
                    break
            else:
                adm = self.engine.admission.admit(
                    spec.description or node.name, plan,
                    smoke=SmokeTest(args=dict(first_args), expect=first_expect),
                    name=node.name)
                if adm.ok:
                    try:
                        rec = self.engine.capabilities.get(adm.capability_id)
                        if rec is not None:
                            self.engine.admission._register_capability_as_primitive(
                                adm.capability_id, rec)
                    except Exception:
                        pass
                    ok, detail = self._heldout_admission_gate(spec, adm.capability_id)
                    if ok:
                        return True, (
                            f"induced constant covering {len(table)} keys; "
                            f"admitted {adm.capability_id} ({detail})"), adm.capability_id
                    self._reject_capability(adm.capability_id, detail)
            # fall through to discrete_lookup if constant path fails
        plan = {
            "params": {key: "any"},
            "steps": [{"id": "lookup", "op": "discrete_lookup",
                       "args": {"table": table, "key": {"$param": key}}}],
            "output": {"$step": "lookup"},
        }
        if self.engine.primitives.get("discrete_lookup") is None:
            from swarm_engine.primitives.core import Primitive, Effect, ANY
            from swarm_engine.primitives.discrete_lookup import discrete_lookup as _dl
            self.engine.primitives.register(Primitive(
                name="discrete_lookup", family="builtin", fn=_dl,
                inputs={"table": ANY, "key": ANY}, output=ANY,
                effects=(Effect.PURE,)), overwrite=True)
        from swarm_engine.synthesis.admission import SmokeTest
        first_args, first_expect = spec.examples[0]
        # Behavioural check on all examples via Composer before admit
        for args, expect in spec.examples:
            run = self.engine.composer.execute_sync(plan, dict(args))
            if not (run.get("success") and run.get("value") == expect):
                return False, (f"induced table fails behavioural check on "
                               f"{args}: got {run.get('value')!r}"), ""
        adm = self.engine.admission.admit(
            spec.description or node.name, plan,
            smoke=SmokeTest(args=dict(first_args), expect=first_expect),
            name=node.name)
        if not adm.ok:
            return False, f"admission refused: {adm.stage} {adm.reasons}", ""
        try:
            rec = self.engine.capabilities.get(adm.capability_id)
            if rec is not None:
                self.engine.admission._register_capability_as_primitive(
                    adm.capability_id, rec)
        except Exception:
            pass
        # 2026-09-19: the experiment/discrete_lookup path must pass the
        # same held-out gate as generate. A lookup memorized from all
        # examples trivially passes a split-based check; the gate's
        # synthetic unseen-input check rejects non-generalizing tables.
        ok, detail = self._heldout_admission_gate(spec, adm.capability_id)
        if not ok:
            self._reject_capability(adm.capability_id, detail)
            return False, f"held-out admission rejected: {detail}", ""
        return True, (f"induced discrete_lookup covering {len(table)} keys; "
                      f"admitted {adm.capability_id} ({detail})"), adm.capability_id

    async def _try_delegate(self, node, spec, delegate) -> Tuple[bool, str, str]:
        if delegate is None:
            return False, "no delegate available", ""
        # O16: the delegate's identity is bound (provenance only) -- its
        # candidates still face the same independent validation as any
        # other source. In bound mode the driver must authenticate as the
        # delegate's producer; an unattributed delegate is REFUSED as a
        # strategy (other strategies are still tried), never silently used.
        try:
            source = DelegatingSource(
                delegate,
                producer_id=self._delegate_producer_id,
                token=self._delegate_token,
                oracle_registry=getattr(self.engine, "oracle_registry", None),
                engine_oracle=getattr(self.engine, "oracle", None))
        except Exception as exc:
            from swarm_engine.governance.oracle_binding import (
                OracleBindingError)
            if isinstance(exc, OracleBindingError):
                return False, f"delegate refused: {exc}", ""
            raise
        candidates = source.search(node.requirement)
        if not candidates:
            return False, "delegate returned nothing", ""
        validator = IndependentValidator(
            self.engine.acquisition.process_sandbox.run,
            oracle_registry=self.engine.oracle_registry,
            engine_oracle=self.engine.oracle)
        cases = [Case(args=dict(a), expect=v) for a, v in spec.examples[:1]]
        for candidate in candidates:
            scan = self.engine.acquisition.scanner.scan(candidate)
            if not scan.passed:
                continue
            verdict = validator.validate(candidate.code, candidate.entrypoint, spec, cases)
            if verdict.admitted:
                capability_id = stable_code_id("del", node.name, candidate.code)
                from swarm_engine.governance.provenance import Origin, ProvenanceRecord, TrustLevel
                self.engine.provenance.record(ProvenanceRecord(
                    capability_id=capability_id, origin=Origin.ACQUIRED,
                    trust=TrustLevel.TESTED, source="delegated"))
                registered = self.engine._register_acquired(
                    node.name, candidate, capability_id, input_names=spec.input_names)
                return registered, "delegate's candidate independently validated", capability_id
        return False, "no delegated candidate survived validation", ""

    async def _try_retrieve(self, node, spec, delegate) -> Tuple[bool, str, str]:
        """Retrieve via NetworkSource through the acquisition pipeline.

        Path: build requirement → NetworkSource.search (governor-gated) →
        scan/sandbox/test → admit/register. No fabricated candidates: if the
        fetcher is unwired, the governor denies NETWORK, or the index returns
        nothing acceptable, this reports the concrete failure.
        """
        from swarm_engine.acquisition.pipeline import (
            NetworkSource, CapabilityRequirement, AcquisitionTest,
        )
        sources = list(getattr(self.engine.acquisition, "sources", []) or [])
        net = next((s for s in sources if isinstance(s, NetworkSource)), None)
        if net is None:
            return False, "no NetworkSource registered on acquisition pipeline", ""
        if net.fetcher is None:
            return False, "NetworkSource.fetcher not wired", ""

        req_name = getattr(node, "name", None) or getattr(spec, "name", None) or "retrieve"
        desc = getattr(spec, "description", None) or req_name
        effects = list(getattr(spec, "required_effects", None) or [])
        examples = list(getattr(spec, "examples", None) or [])
        param_names = None
        if examples and isinstance(examples[0][0], dict):
            param_names = tuple(sorted(examples[0][0].keys()))
        requirement = CapabilityRequirement(
            name=str(req_name),
            description=str(desc),
            required_effects=effects,
            examples=examples or None,
            param_names=param_names,
        )
        tests = None
        if examples:
            tests = [
                AcquisitionTest(args=dict(a), expect=e) for a, e in examples
            ]
        try:
            result = self.engine.acquisition.acquire(requirement, tests=tests)
        except Exception as exc:
            return False, f"retrieve acquire raised: {type(exc).__name__}: {exc}", ""

        if not getattr(result, "accepted", False):
            why = "; ".join(getattr(result, "reasons", None) or []) or result.stage
            return False, f"retrieve not admitted ({result.stage}): {why}", ""

        cap_id = result.capability_id
        # Bind goal text for fresh-process lookup when available.
        try:
            goal = getattr(spec, "description", None) or req_name
            self.engine.capabilities.bind_goal(goal, cap_id)
        except Exception:
            pass
        try:
            self.engine.capabilities.bind_goal(str(req_name), cap_id)
        except Exception:
            pass
        return True, (
            f"retrieved via NetworkSource endpoint={net.endpoint!r}; "
            f"admitted {cap_id}; registered={result.registered}"
        ), cap_id
