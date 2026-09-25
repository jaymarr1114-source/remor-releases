"""Mediated agent execution.

AgentRunner.execute() enforces, in order:
1. require_active: the assignment must be ACTIVE and bound to this agent.
2. Serialization boundary: the task is JSON round-tripped; the substrate
   receives a plain dict, never a registry handle or a live object.
3. Lifecycle: agent ASSIGNED -> EXECUTING before the substrate runs.
4. Substrate exceptions -> agent FAILED + chained event, then raise.
5. Result must be JSON-serializable, else ExecutionError + FAILED.
6. CAPABILITY GATE (fail closed): result["claimed_capabilities"] must be
   present and a list; every claimed capability must fnmatch at least one
   allowed_capability_patterns entry from the assignment's authority scope.
   Missing key or unmatched claim -> AuthorityError + agent FAILED.
7. WorkProduct built (content-addressed wp_id), state SUBMITTED;
   agent EXECUTING -> SUBMITTED.

Custody: the runner holds agent_id -> producer token in memory (REMOR is
custodian). Substrates never see tokens. Provenance is recorded with
supplier_id = the agent's producer and evaluator = remor:engine.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import platform
import sys
from typing import Any, Dict

from swarm_engine.agent_org.assignment import AssignmentManager
from swarm_engine.agent_org.exceptions import (
    AuthorityError,
    ExecutionError,
)
from swarm_engine.agent_org.registry import AgentRegistry
from swarm_engine.agent_org.store import OrgStore, digest, now
from swarm_engine.agent_org.work_product import (
    WorkProduct,
    persist_work_product,
)

_ENGINE = "remor:engine"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str)


class AgentRunner:
    def __init__(self, store: OrgStore, assignments: AssignmentManager,
                 agents: AgentRegistry, engine: Any):
        self.store = store
        self.assignments = assignments
        self.agents = agents
        self.engine = engine
        # Custody map: agent_id -> producer token, in memory only.
        self._custody: Dict[str, str] = {}

    def _fail(self, agent_id: str, reason: str) -> None:
        self.agents.transition(agent_id, "FAILED", actor=_ENGINE,
                               reason=reason)

    def execute(self, assignment_id: str, task: Dict[str, Any]) -> str:
        asg = self.assignments.get(assignment_id)
        self.assignments.require_active(assignment_id, asg.agent_id)
        agent_id = asg.agent_id
        agent = self.agents.get(agent_id)

        # Custody: REMOR takes custody of the producer token for this run.
        self._custody[agent_id] = self.agents.custody_token(agent_id)

        # Serialization boundary: substrate gets a dict, never a handle.
        # This check runs BEFORE the agent leaves ASSIGNED: a bad task is
        # the caller's fault, so the agent stays ASSIGNED (reusable) and
        # no FAILED transition is attempted (ASSIGNED -> FAILED is not a
        # legal lifecycle step).
        try:
            task_rt = json.loads(json.dumps(task))
        except (TypeError, ValueError) as exc:
            raise ExecutionError(
                f"task for {assignment_id} is not JSON-serializable: "
                f"{exc}")

        self.agents.transition(agent_id, "EXECUTING", actor=_ENGINE,
                               reason=f"assignment {assignment_id} executing")
        substrate = self.agents.get_substrate(agent_id)
        try:
            result = substrate.run(task_rt)
        except Exception as exc:
            self._fail(agent_id,
                       f"substrate {substrate.substrate_id} raised "
                       f"{type(exc).__name__}: {exc}")
            if isinstance(exc, (AuthorityError, ExecutionError)):
                raise
            raise ExecutionError(
                f"substrate {substrate.substrate_id} failed: {exc}") from exc

        try:
            json.dumps(result)
        except (TypeError, ValueError) as exc:
            self._fail(agent_id, f"result not JSON-serializable: {exc}")
            raise ExecutionError(
                f"substrate result for {assignment_id} is not "
                f"JSON-serializable: {exc}")

        # -- capability gate (fail closed) --------------------------------
        claimed = result.get("claimed_capabilities")
        patterns = asg.authority_scope.get("allowed_capability_patterns", [])
        if not isinstance(claimed, list):
            self._fail(agent_id, "result missing claimed_capabilities list")
            raise AuthorityError(
                f"substrate result for {assignment_id} is missing "
                f"'claimed_capabilities': refused (fail closed)")
        for cap in claimed:
            if not any(fnmatch.fnmatch(str(cap), pat) for pat in patterns):
                self._fail(
                    agent_id,
                    f"capability {cap!r} not covered by {patterns}")
                raise AuthorityError(
                    f"capability {cap!r} claimed by substrate result is "
                    f"outside assignment {assignment_id} authority scope "
                    f"{patterns}: refused")

        # -- build the work product ---------------------------------------
        implementation = result.get("implementation", "")
        code_digest = digest(implementation) if isinstance(implementation,
                                                          str) else ""
        wp_id = "wp_" + digest(
            _canonical(result) + assignment_id + agent_id)[:16]
        artifacts = []
        if isinstance(implementation, str) and implementation:
            artifacts.append({"name": "codec.py", "code": implementation,
                              "digest": code_digest})
        wp = WorkProduct(
            wp_id=wp_id, assignment_id=assignment_id, agent_id=agent_id,
            template_id=agent.template_id,
            template_version=agent.template_version,
            substrate_id=substrate.substrate_id,
            entrypoint=str(result.get("entrypoint", "")),
            input_digest=digest(_canonical(task_rt)),
            outputs={k: v for k, v in result.items()
                     if k != "implementation"} | {
                "problem_class": str(task_rt.get("problem_class",
                                                 "byte_codec"))},
            artifacts=artifacts,
            evidence_refs={
                "supplier_id": agent.producer_id,
                "evaluator": _ENGINE,
                "custody": ("REMOR custodian holds producer token; "
                            "substrate never saw it"),
                "claimed_capabilities": list(claimed),
            },
            environment={"python": platform.python_version(),
                         "platform": sys.platform},
            self_reported_success=result.get("self_reported_success"),
            state="SUBMITTED", created_at=now())
        persist_work_product(self.store, wp)
        self.store.insert("ao_work_product_events", {
            "wp_id": wp_id, "from_state": "", "to_state": "SUBMITTED",
            "actor": _ENGINE,
            "reason": f"executed under assignment {assignment_id}",
            "created_at": now()})
        self.agents.transition(agent_id, "SUBMITTED", actor=_ENGINE,
                               reason=f"work product {wp_id} submitted")
        return wp_id

    def custody_held(self, agent_id: str) -> bool:
        """Whether the runner currently holds custody for this agent."""
        return agent_id in self._custody

    def release_custody(self, agent_id: str) -> None:
        self._custody.pop(agent_id, None)
