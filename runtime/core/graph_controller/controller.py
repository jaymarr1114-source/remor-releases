"""GraphController — the hierarchy's fourth level.

Executive -> Loop Controller -> Microcontroller -> GraphController -> Graph.

A microcontroller owns a bounded objective. To accomplish it, it must navigate
and operate some structural graph (nodes / edges / dependencies / transitions).
The GraphController is the mediation layer between microcontroller intent and
graph traversal. It answers: "How do I navigate and operate the structural
graph necessary to resolve this objective?"

It is deliberately NOT an executor: like the swarm Scheduler, it owns no
execution logic. The microcontroller (or its loop) does the work; the
controller decides WHAT may be operated next, in WHAT order, and WITHIN what
bounds. Cursor model:

    region = gc.open_region(mc_id, graph_id, objective, max_operations=...)
    while True:
        nxt = gc.next_operable(region_id)     # controller decides operability
        if not nxt.ok: break
        value = do_the_work(nxt.node_id)      # the microcontroller's own logic
        gc.record_result(region_id, nxt.node_id, success=True, value=value)
    summary = gc.close_region(region_id)

Genuine mediation (not a pass-through):
  - region selection: the controller maps the objective to a node set by
    relevance scoring, then expands to dependency closure so the region is
    self-sufficient. Fail-closed on empty regions; operation budgets are
    enforced during traversal (bounded partial progress).
  - traversal bounds: max_operations enforced per region; excess refused.
  - dependency-ordered operability: a node is operable only when its
    dependencies recorded success. Failed nodes mark dependents unreachable.
  - recursion: a region may open subordinate GraphControllers over
    sub-regions (theory: further decomposition).

Refusals are values, never exceptions (codebase convention).

Visibility boundary (theory sec. 10): graph traversal stays below the loop
level. The controller reports aggregates and results to the microcontroller;
nothing here is executive-facing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Set, Tuple


# ---------------------------------------------------------------------------
# Refusal-as-values (mirrors microcontroller substrate conventions)
# ---------------------------------------------------------------------------

R_UNKNOWN_GRAPH = "unknown_graph"
R_UNKNOWN_REGION = "unknown_region"
R_REGION_CLOSED = "region_closed"
R_NO_MATCHING_NODES = "no_matching_nodes"
R_REGION_BOUND_EXCEEDED = "region_bound_exceeded"
R_OUT_OF_REGION = "out_of_region"
R_NODE_NOT_OPERABLE = "node_not_operable"
R_ALREADY_RECORDED = "already_recorded"
R_OUTSIDE_PARENT_REGION = "outside_parent_region"
R_INVALID_OBJECTIVE = "invalid_objective"
R_INVALID_BOUND = "invalid_bound"


@dataclass(frozen=True)
class Refusal:
    reason: str
    message: str

    def as_dict(self) -> Dict[str, str]:
        return {"reason": self.reason, "message": self.message}


# ---------------------------------------------------------------------------
# Structural graph protocol + TaskGraph adapter
# ---------------------------------------------------------------------------

class StructuralGraph(Protocol):
    """Minimal structural surface the controller needs. Any node/edge graph
    can be adapted; the controller never touches graph internals directly."""

    @property
    def graph_id(self) -> str: ...
    def node_ids(self) -> List[str]: ...
    def node_goal(self, node_id: str) -> str: ...
    def dependencies(self, node_id: str) -> List[str]: ...


class TaskGraphAdapter:
    """Adapts the real swarm TaskGraph (runtime/core/taskgraph.py) to the
    StructuralGraph protocol. The adapter is mechanical; all decisions live
    in the GraphController."""

    def __init__(self, task_graph, graph_id: Optional[str] = None):
        self._g = task_graph
        self._id = graph_id or getattr(task_graph, "root_goal", "graph")

    @property
    def graph_id(self) -> str:
        return self._id

    def node_ids(self) -> List[str]:
        return list(self._g.nodes.keys())

    def node_goal(self, node_id: str) -> str:
        return self._g.nodes[node_id].goal

    def dependencies(self, node_id: str) -> List[str]:
        return list(self._g.nodes[node_id].depends_on)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RegionResult:
    ok: bool
    region_id: Optional[str] = None
    node_ids: Tuple[str, ...] = ()
    refusal: Optional[Refusal] = None


@dataclass
class NodeResult:
    ok: bool
    node_id: Optional[str] = None
    goal: Optional[str] = None
    status: str = ""          # "ready" | "none_ready" (ok=False only)
    refusal: Optional[Refusal] = None


@dataclass
class RecordResult:
    ok: bool
    node_id: Optional[str] = None
    refusal: Optional[Refusal] = None


@dataclass
class RegionSummary:
    region_id: str
    graph_id: str
    objective: str
    nodes_total: int
    operated: int
    succeeded: int
    failed: int
    unreachable: int
    bound_hit: bool
    values: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "region_id": self.region_id,
            "graph_id": self.graph_id,
            "objective": self.objective,
            "nodes_total": self.nodes_total,
            "operated": self.operated,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "unreachable": self.unreachable,
            "bound_hit": self.bound_hit,
            "values": dict(self.values),
        }


# ---------------------------------------------------------------------------
# Internal region record
# ---------------------------------------------------------------------------

_NODE_PENDING = "pending"
_NODE_DONE = "done"
_NODE_FAILED = "failed"
_NODE_UNREACHABLE = "unreachable"


class _Region:
    __slots__ = ("region_id", "graph_id", "mc_id", "objective", "node_ids",
                 "max_operations", "ops", "state", "bound_hit", "values",
                 "parent_id", "children")

    def __init__(self, region_id: str, graph_id: str, mc_id: str,
                 objective: str, node_ids: List[str], max_operations: int,
                 parent_id: Optional[str] = None):
        self.region_id = region_id
        self.graph_id = graph_id
        self.mc_id = mc_id
        self.objective = objective
        self.node_ids = list(node_ids)
        self.max_operations = max_operations
        self.ops = 0
        self.state: Dict[str, str] = {n: _NODE_PENDING for n in node_ids}
        self.bound_hit = False
        self.values: Dict[str, object] = {}
        self.parent_id = parent_id
        self.children: List[str] = []


# ---------------------------------------------------------------------------
# Relevance scoring (the region-selection decision)
# ---------------------------------------------------------------------------

def _tokens(text: str) -> Set[str]:
    toks: Set[str] = set()
    for raw in text.lower().split():
        t = "".join(c for c in raw if c.isalnum())
        if not t:
            continue
        # crude stem so "analyze"/"analyzed"/"analyzing" meet
        for suf in ("ing", "ed"):
            if len(t) > len(suf) + 2 and t.endswith(suf):
                t = t[: -len(suf)]
                break
        if len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        toks.add(t)
    return toks


_STOP = {"a", "an", "the", "of", "to", "for", "from", "and", "or", "then",
        "with", "on", "in", "is", "it", "its"}


def _relevance(objective_tokens: Set[str], goal_tokens: Set[str]) -> float:
    obj = objective_tokens - _STOP
    if not obj:
        return 0.0
    return len(obj & (goal_tokens - _STOP)) / len(obj)


# ---------------------------------------------------------------------------
# GraphController
# ---------------------------------------------------------------------------

class GraphController:
    """Mediates one microcontroller's navigation and operation of structural
    graphs. See module docstring for the model."""

    _RELEVANCE_FLOOR = 0.34  # at least ~a third of the objective's tokens

    def __init__(self):
        self._graphs: Dict[str, StructuralGraph] = {}
        self._regions: Dict[str, _Region] = {}
        self._seq = 0
        self._children: List["GraphController"] = []

    @classmethod
    def _with_shared_graphs(cls, graphs: Dict[str, StructuralGraph]
                             ) -> "GraphController":
        inst = cls()
        inst._graphs = graphs  # shared read-only attachment registry
        return inst

    # -- graph attachment -------------------------------------------------

    def attach_graph(self, graph: StructuralGraph) -> str:
        """Register a structural graph. Idempotent per graph_id."""
        self._graphs[graph.graph_id] = graph
        return graph.graph_id

    # -- region lifecycle ---------------------------------------------------

    def open_region(self, mc_id: str, graph_id: str, objective: str, *,
                    max_operations: int = 64) -> RegionResult:
        if not isinstance(objective, str) or not objective.strip():
            return RegionResult(ok=False, refusal=Refusal(
                R_INVALID_OBJECTIVE, "objective must be a non-empty string"))
        if not (isinstance(max_operations, int) and max_operations > 0):
            return RegionResult(ok=False, refusal=Refusal(
                R_INVALID_BOUND, "max_operations must be a positive int"))
        graph = self._graphs.get(graph_id)
        if graph is None:
            return RegionResult(ok=False, refusal=Refusal(
                R_UNKNOWN_GRAPH, f"graph {graph_id!r} not attached"))

        node_ids = self._select_region(graph, objective)
        if not node_ids:
            return RegionResult(ok=False, refusal=Refusal(
                R_NO_MATCHING_NODES,
                f"no graph nodes relevant to objective {objective!r}"))
        # NOTE: no refusal when the region needs more nodes than
        # max_operations allows. The budget is enforced during operation
        # (bounded partial progress, reported via bound_hit) rather than
        # by refusing to start — mirroring the substrate's
        # admission-at-spawn + exhaustion-via-charge shape.

        self._seq += 1
        rid = f"gr-{self._seq:06d}"
        self._regions[rid] = _Region(rid, graph_id, mc_id, objective,
                                    node_ids, max_operations)
        return RegionResult(ok=True, region_id=rid,
                            node_ids=tuple(node_ids))

    def open_subregion(self, region_id: str, node_ids: List[str], *,
                       max_operations: int = 64
                       ) -> Tuple["GraphController", RegionResult]:
        """Open a subordinate controller over a sub-region (theory: recursive
        decomposition — a graph region requiring further decomposition gets
        its own GraphController). node_ids must lie within the parent region.
        Returns (child_controller, region_result); operate through the child.
        """
        parent = self._regions.get(region_id)
        if parent is None:
            return None, RegionResult(ok=False, refusal=Refusal(
                R_UNKNOWN_REGION, f"region {region_id!r} unknown"))
        if not (isinstance(max_operations, int) and max_operations > 0):
            return None, RegionResult(ok=False, refusal=Refusal(
                R_INVALID_BOUND, "max_operations must be a positive int"))
        parent_set = set(parent.node_ids)
        outside = [n for n in node_ids if n not in parent_set]
        if outside:
            return None, RegionResult(ok=False, refusal=Refusal(
                R_OUTSIDE_PARENT_REGION,
                f"nodes {outside} lie outside parent region {region_id!r}"))
        child = GraphController._with_shared_graphs(self._graphs)
        self._seq += 1
        rid = f"gr-{self._seq:06d}"
        child._regions[rid] = _Region(rid, parent.graph_id, parent.mc_id,
                                     parent.objective + " [subregion]",
                                     list(node_ids), max_operations,
                                     parent_id=region_id)
        child._seq = self._seq
        self._children.append(child)
        parent.children.append(rid)
        return child, RegionResult(ok=True, region_id=rid,
                                   node_ids=tuple(node_ids))

    def close_region(self, region_id: str) -> RegionSummary | Refusal:
        region = self._regions.get(region_id)
        if region is None:
            return Refusal(R_UNKNOWN_REGION, f"region {region_id!r} unknown")
        states = region.state
        summary = RegionSummary(
            region_id=region.region_id, graph_id=region.graph_id,
            objective=region.objective, nodes_total=len(region.node_ids),
            operated=region.ops,
            succeeded=sum(1 for s in states.values() if s == _NODE_DONE),
            failed=sum(1 for s in states.values() if s == _NODE_FAILED),
            unreachable=sum(1 for s in states.values()
                            if s == _NODE_UNREACHABLE),
            bound_hit=region.bound_hit, values=dict(region.values))
        del self._regions[region_id]
        return summary

    # -- navigation: the controller decides operability ----------------------

    def next_operable(self, region_id: str) -> NodeResult:
        region = self._regions.get(region_id)
        if region is None:
            return NodeResult(ok=False, refusal=Refusal(
                R_UNKNOWN_REGION, f"region {region_id!r} unknown"))
        graph = self._graphs[region.graph_id]
        for nid in region.node_ids:  # deterministic: region order
            if region.state[nid] != _NODE_PENDING:
                continue
            deps = graph.dependencies(nid)
            dep_states = [region.state[d] for d in deps if d in region.state]
            if any(s == _NODE_FAILED or s == _NODE_UNREACHABLE
                   for s in dep_states):
                region.state[nid] = _NODE_UNREACHABLE
                continue
            if all(s == _NODE_DONE for s in dep_states):
                return NodeResult(ok=True, node_id=nid,
                                  goal=graph.node_goal(nid), status="ready")
        return NodeResult(ok=False, status="none_ready")

    def record_result(self, region_id: str, node_id: str, *, success: bool,
                      value: object = None, error: Optional[str] = None
                      ) -> RecordResult:
        region = self._regions.get(region_id)
        if region is None:
            return RecordResult(ok=False, refusal=Refusal(
                R_UNKNOWN_REGION, f"region {region_id!r} unknown"))
        if node_id not in region.state:
            return RecordResult(ok=False, refusal=Refusal(
                R_OUT_OF_REGION,
                f"node {node_id!r} is not in region {region_id!r}"))
        if region.state[node_id] != _NODE_PENDING:
            return RecordResult(ok=False, refusal=Refusal(
                R_ALREADY_RECORDED,
                f"node {node_id!r} already {region.state[node_id]}"))
        if not self._deps_satisfied(region, node_id):
            return RecordResult(ok=False, refusal=Refusal(
                R_NODE_NOT_OPERABLE,
                f"node {node_id!r} dependencies not satisfied"))
        if region.ops >= region.max_operations:
            region.bound_hit = True
            return RecordResult(ok=False, refusal=Refusal(
                R_REGION_BOUND_EXCEEDED,
                f"region {region_id!r} bound max_operations="
                f"{region.max_operations} reached"))
        region.ops += 1
        if success:
            region.state[node_id] = _NODE_DONE
            region.values[node_id] = value
        else:
            region.state[node_id] = _NODE_FAILED
            region.values[node_id] = {"error": error or "node failed"}
            self._mark_unreachable(region, node_id)
        return RecordResult(ok=True, node_id=node_id)

    def region_view(self, region_id: str) -> Dict[str, object] | Refusal:
        """Aggregates only — no traversal internals leak above the loop level."""
        region = self._regions.get(region_id)
        if region is None:
            return Refusal(R_UNKNOWN_REGION, f"region {region_id!r} unknown")
        states = region.state
        return {
            "region_id": region.region_id,
            "objective": region.objective,
            "nodes_total": len(region.node_ids),
            "operations_used": region.ops,
            "operations_budget": region.max_operations,
            "pending": sum(1 for s in states.values() if s == _NODE_PENDING),
            "succeeded": sum(1 for s in states.values() if s == _NODE_DONE),
            "failed": sum(1 for s in states.values() if s == _NODE_FAILED),
        }

    # -- internals -----------------------------------------------------------

    def _select_region(self, graph: StructuralGraph,
                       objective: str) -> List[str]:
        """Relevance scoring + dependency closure. The controller's real
        decision: which nodes form the operating region for this objective."""
        obj_toks = _tokens(objective)
        scored: List[Tuple[float, str]] = []
        for nid in graph.node_ids():
            r = _relevance(obj_toks, _tokens(graph.node_goal(nid)))
            if r >= self._RELEVANCE_FLOOR:
                scored.append((r, nid))
        # dependency closure: a region must be self-sufficient
        selected: Set[str] = {nid for _, nid in scored}
        changed = True
        while changed:
            changed = False
            for nid in list(selected):
                for dep in graph.dependencies(nid):
                    if dep not in selected:
                        selected.add(dep)
                        changed = True
        # deterministic order: by relevance desc, then node id
        rank = {nid: r for r, nid in scored}
        return sorted(selected, key=lambda n: (-rank.get(n, 0.0), n))

    def _deps_satisfied(self, region: _Region, node_id: str) -> bool:
        graph = self._graphs[region.graph_id]
        for dep in graph.dependencies(node_id):
            if dep in region.state and region.state[dep] != _NODE_DONE:
                return False
        return True

    def _mark_unreachable(self, region: _Region, failed_id: str) -> None:
        graph = self._graphs[region.graph_id]
        changed = True
        while changed:
            changed = False
            for nid, st in region.state.items():
                if st != _NODE_PENDING:
                    continue
                for dep in graph.dependencies(nid):
                    if dep in region.state and region.state[dep] in (
                            _NODE_FAILED, _NODE_UNREACHABLE):
                        region.state[nid] = _NODE_UNREACHABLE
                        changed = True
                        break
