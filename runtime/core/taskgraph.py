"""
swarm_engine/core/taskgraph.py

Generic task decomposition and scheduling.

The engine previously recognised that a goal was composite, built a list of
subtasks, and then stopped with "a generic subtask scheduler is not yet
implemented". This module is that scheduler. It turns a composite goal into a
dependency graph, runs independent nodes concurrently, threads results from
one node into the next, and reports partial progress when a node fails rather
than losing the work that already succeeded.

Three things are deliberate:

1. Decomposition is recursive but bounded. A subtask may itself decompose,
   which is what lets a goal several levels deep resolve. Unbounded recursion
   on a self-similar goal is a task explosion, so depth and total node count
   are hard limits, not advisory ones.

2. Failure is local. One failed node marks its dependents unreachable and
   leaves every independent branch to finish. A composite goal that half
   worked reports which half, because "failed" without that is not actionable.

3. The scheduler owns no execution logic. It calls back into whatever the
   engine passes as `runner`, so a node is executed by the same arbitration →
   synthesis → verification path as a top-level task. The scheduler decides
   *when* a node runs, never *how*.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set


class NodeState(Enum):
    PENDING = "pending"        # waiting on dependencies
    READY = "ready"            # dependencies satisfied, not yet started
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNREACHABLE = "unreachable"  # an upstream dependency failed


@dataclass
class TaskNode:
    node_id: str
    goal: str
    payload: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    depth: int = 0
    state: NodeState = NodeState.PENDING
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "goal": self.goal,
            "depends_on": list(self.depends_on),
            "depth": self.depth,
            "state": self.state.value,
            "value": (self.result or {}).get("value"),
            "error": self.error,
        }


class TaskGraphError(Exception):
    pass


class TaskGraph:
    """A dependency graph of subtasks with cycle and bound checking."""

    def __init__(self, root_goal: str, max_nodes: int = 64):
        self.root_goal = root_goal
        self.nodes: Dict[str, TaskNode] = {}
        self.max_nodes = max_nodes
        self._seq = 0

    def add(self, goal: str, payload: Optional[Dict[str, Any]] = None,
            depends_on: Optional[List[str]] = None, depth: int = 0) -> TaskNode:
        if len(self.nodes) >= self.max_nodes:
            raise TaskGraphError(
                f"task graph exceeded {self.max_nodes} nodes while expanding "
                f"{self.root_goal!r}; refusing to expand further"
            )
        self._seq += 1
        node_id = f"n{self._seq}"
        for dep in depends_on or []:
            if dep not in self.nodes:
                raise TaskGraphError(f"node {node_id} depends on unknown node {dep!r}")
        node = TaskNode(node_id=node_id, goal=goal, payload=dict(payload or {}),
                        depends_on=list(depends_on or []), depth=depth)
        self.nodes[node_id] = node
        return node

    def validate(self) -> None:
        """Reject cycles before anything executes. A cycle in a dependency
        graph is a deadlock at run time, and a scheduler that discovers it by
        hanging is worse than one that refuses up front."""
        colour: Dict[str, int] = {n: 0 for n in self.nodes}  # 0 unvisited, 1 open, 2 closed

        def visit(nid: str, trail: List[str]) -> None:
            if colour[nid] == 1:
                cycle = " -> ".join(trail + [nid])
                raise TaskGraphError(f"dependency cycle: {cycle}")
            if colour[nid] == 2:
                return
            colour[nid] = 1
            for dep in self.nodes[nid].depends_on:
                visit(dep, trail + [nid])
            colour[nid] = 2

        for nid in self.nodes:
            visit(nid, [])

    def ready_nodes(self) -> List[TaskNode]:
        """Nodes whose dependencies have all completed."""
        out = []
        for node in self.nodes.values():
            if node.state is not NodeState.PENDING:
                continue
            deps = [self.nodes[d] for d in node.depends_on]
            if any(d.state in (NodeState.FAILED, NodeState.UNREACHABLE) for d in deps):
                node.state = NodeState.UNREACHABLE
                node.error = "an upstream subtask failed"
                continue
            if all(d.state is NodeState.COMPLETED for d in deps):
                out.append(node)
        return out

    def unfinished(self) -> bool:
        return any(n.state in (NodeState.PENDING, NodeState.READY, NodeState.RUNNING)
                   for n in self.nodes.values())

    def terminal_nodes(self) -> List[TaskNode]:
        """Nodes nothing else depends on: the graph's outputs."""
        depended: Set[str] = set()
        for node in self.nodes.values():
            depended.update(node.depends_on)
        return [n for n in self.nodes.values() if n.node_id not in depended]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "root_goal": self.root_goal,
            "nodes": [n.as_dict() for n in self.nodes.values()],
        }


# ---------------------------------------------------------------------------
# DECOMPOSITION
# ---------------------------------------------------------------------------

def split_composite(goal: str) -> List[str]:
    """Split a composite goal into ordered stages.

    ':' is the engine's existing composite syntax and stays authoritative.
    ' then ' is accepted because it is how a composite goal is actually
    phrased, and the arbitrator already treats such goals as decomposable.
    """
    if ":" in goal:
        return [p.strip() for p in goal.split(":") if p.strip()]
    lowered = goal.lower()
    if " then " in lowered:
        parts, rest = [], goal
        while True:
            idx = rest.lower().find(" then ")
            if idx < 0:
                parts.append(rest.strip())
                break
            parts.append(rest[:idx].strip())
            rest = rest[idx + len(" then "):]
        return [p for p in parts if p]
    return [goal]


def is_composite(goal: str) -> bool:
    """Does this goal describe more than one stage?

    Arbitration and decomposition must agree on this, or the engine routes a
    goal to DECOMPOSE that the splitter then treats as atomic (or worse, the
    reverse: a genuinely multi-stage goal is handed to the planner as a single
    intent and quietly resolves to whichever stage the templates happened to
    match). One definition, used by both.
    """
    return len(split_composite(goal)) > 1


def build_graph(goal: str, payload: Optional[Dict[str, Any]] = None,
                max_depth: int = 3, max_nodes: int = 64) -> TaskGraph:
    """Expand a goal into a dependency graph, recursing into stages that are
    themselves composite.

    Stages are chained: stage N depends on stage N-1, because composite goals
    are written as pipelines. When a stage decomposes further, its own last
    node becomes the stage's output so the chain stays intact.
    """
    graph = TaskGraph(goal, max_nodes=max_nodes)

    def expand(text: str, payload: Dict[str, Any], depth: int,
               upstream: Optional[str]) -> str:
        """Add nodes for `text`; return the id of the node that produces its
        result, so callers can depend on it."""
        stages = split_composite(text)

        # Not composite, or we've hit the recursion bound: one node.
        if len(stages) == 1 or depth >= max_depth:
            node = graph.add(text, payload=payload,
                             depends_on=[upstream] if upstream else [],
                             depth=depth)
            return node.node_id

        previous = upstream
        for stage in stages:
            # Only the first stage receives the caller's payload; later stages
            # consume the previous stage's output, which the scheduler injects.
            stage_payload = payload if previous is upstream else {}
            previous = expand(stage, stage_payload, depth + 1, previous)
        return previous

    expand(goal, dict(payload or {}), 0, None)
    graph.validate()
    return graph


# ---------------------------------------------------------------------------
# SCHEDULER
# ---------------------------------------------------------------------------

# A node runner takes (goal, payload) and returns an engine-shaped result dict.
NodeRunner = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class Scheduler:
    """Executes a TaskGraph, running independent nodes concurrently."""

    def __init__(self, runner: NodeRunner, max_parallel: int = 4):
        self.runner = runner
        self.max_parallel = max_parallel

    async def run(self, graph: TaskGraph) -> Dict[str, Any]:
        graph.validate()
        semaphore = asyncio.Semaphore(self.max_parallel)

        async def run_node(node: TaskNode) -> None:
            async with semaphore:
                node.state = NodeState.RUNNING
                payload = self._payload_for(graph, node)
                try:
                    result = await self.runner(node.goal, payload)
                except Exception as exc:  # a runner fault must not kill the graph
                    node.state = NodeState.FAILED
                    node.error = f"{type(exc).__name__}: {exc}"
                    return
                node.result = result
                if result.get("success"):
                    node.state = NodeState.COMPLETED
                else:
                    node.state = NodeState.FAILED
                    node.error = str(result.get("error") or "subtask failed")

        while graph.unfinished():
            ready = graph.ready_nodes()
            if not ready:
                # Nothing runnable and nothing running: everything left is
                # blocked behind a failure. ready_nodes() has already marked
                # them unreachable, so this is a clean stop, not a hang.
                if not any(n.state is NodeState.RUNNING for n in graph.nodes.values()):
                    break
                await asyncio.sleep(0)
                continue
            await asyncio.gather(*(run_node(n) for n in ready))

        return self._summarise(graph)

    def _payload_for(self, graph: TaskGraph, node: TaskNode) -> Dict[str, Any]:
        """Thread upstream results into this node's payload.

        A stage that declares its own inputs keeps them; otherwise the single
        upstream result is passed through. Values are offered under the common
        input names rather than guessed at from the goal text, so a stage can
        pick up its predecessor's output without the graph needing to know
        which primitive will eventually run.
        """
        payload = dict(node.payload)
        upstream = [graph.nodes[d] for d in node.depends_on
                    if graph.nodes[d].state is NodeState.COMPLETED]
        if not upstream or payload:
            return payload

        value = (upstream[-1].result or {}).get("value")
        if value is None:
            return payload
        if isinstance(value, list):
            payload["values"] = value
        elif isinstance(value, str):
            payload["text"] = value
        payload["input"] = value
        return payload

    def _summarise(self, graph: TaskGraph) -> Dict[str, Any]:
        completed = [n for n in graph.nodes.values() if n.state is NodeState.COMPLETED]
        failed = [n for n in graph.nodes.values() if n.state is NodeState.FAILED]
        unreachable = [n for n in graph.nodes.values() if n.state is NodeState.UNREACHABLE]

        terminals = graph.terminal_nodes()
        value = None
        if terminals and all(t.state is NodeState.COMPLETED for t in terminals):
            outputs = [(t.result or {}).get("value") for t in terminals]
            value = outputs[0] if len(outputs) == 1 else outputs

        return {
            "success": not failed and not unreachable,
            "value": value,
            "nodes_total": len(graph.nodes),
            "nodes_completed": len(completed),
            "nodes_failed": len(failed),
            "nodes_unreachable": len(unreachable),
            # Partial progress is reported explicitly: a composite goal that
            # got three stages in before failing should say so.
            "partial": bool(completed) and bool(failed or unreachable),
            "failures": [{"node": n.node_id, "goal": n.goal, "error": n.error}
                         for n in failed],
            "graph": graph.as_dict(),
        }
