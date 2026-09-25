"""Assignments: scoped authority for agent work.

An assignment binds an agent to an objective under an explicit authority
scope. Grants are issued per-assignment (effect "capability:use", pattern
per allowed_capability_patterns, scope=assignment_id) and revoked when the
assignment completes, expires, or is revoked. Every step is mediated:
require_active() raises AuthorityError unless the assignment is ACTIVE and
bound to the calling agent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.agent_org.exceptions import (
    AuthorityError, LifecycleError, OrgError,
)
from swarm_engine.agent_org.store import OrgStore, digest, now

try:
    from swarm_engine.governance.oracle_binding import EngineOracleHandle
except Exception:  # pragma: no cover
    EngineOracleHandle = None  # type: ignore

CREATED, ACTIVE, COMPLETED, EXPIRED, REVOKED = (
    "CREATED", "ACTIVE", "COMPLETED", "EXPIRED", "REVOKED")

_ASSIGNMENT_FLOW = {
    CREATED: (ACTIVE, REVOKED, EXPIRED),
    ACTIVE: (COMPLETED, EXPIRED, REVOKED),
    COMPLETED: (),
    EXPIRED: (),
    REVOKED: (),
}


@dataclass
class Assignment:
    assignment_id: str
    agent_id: str
    objective: Dict[str, Any]
    constraints: Dict[str, Any]
    authority_scope: Dict[str, Any]
    expected_outputs: Dict[str, Any]
    validation_requirements: Dict[str, Any]
    originating_decision: str
    state: str
    created_at: str


def _require_engine(engine: Any) -> EngineOracleHandle:
    if EngineOracleHandle is None or not isinstance(engine, EngineOracleHandle):
        raise AuthorityError(
            "assignment authority operations require a live EngineOracleHandle")
    if engine.producer_id != "remor:engine":
        raise AuthorityError(
            f"assignment authority requires 'remor:engine', "
            f"got {engine.producer_id!r}")
    return engine


class AssignmentManager:
    def __init__(self, store: OrgStore, oregistry: Any, engine: Any,
                 agents: Any):
        self.store = store
        self.oregistry = oregistry
        self.engine = _require_engine(engine)
        self.agents = agents

    # -- reads ----------------------------------------------------------
    @staticmethod
    def _row_to_assignment(row: Dict[str, Any]) -> Assignment:
        return Assignment(
            assignment_id=row["assignment_id"], agent_id=row["agent_id"],
            objective=json.loads(row["objective_json"]),
            constraints=json.loads(row["constraints_json"]),
            authority_scope=json.loads(row["authority_scope_json"]),
            expected_outputs=json.loads(row["expected_outputs_json"]),
            validation_requirements=json.loads(row["validation_requirements_json"]),
            originating_decision=row["originating_decision"],
            state=row["state"], created_at=row["created_at"])

    def get(self, assignment_id: str) -> Assignment:
        row = self.store.latest("ao_assignments", "assignment_id",
                                assignment_id)
        if row is None:
            raise KeyError(f"unknown assignment {assignment_id!r}")
        return self._row_to_assignment(row)

    def list(self, agent_id: Optional[str] = None,
             state: Optional[str] = None) -> List[Assignment]:
        rows = self.store.rows("ao_assignments")
        latest: Dict[str, Dict] = {}
        for row in rows:
            latest[row["assignment_id"]] = row
        out = [self._row_to_assignment(r) for r in latest.values()]
        if agent_id is not None:
            out = [a for a in out if a.agent_id == agent_id]
        if state is not None:
            out = [a for a in out if a.state == state]
        return out

    # -- lifecycle ------------------------------------------------------
    def _set_state(self, assignment_id: str, to_state: str, actor: str,
                   reason: str = "") -> Assignment:
        asg = self.get(assignment_id)
        if to_state not in _ASSIGNMENT_FLOW[asg.state]:
            raise LifecycleError(
                f"assignment {assignment_id}: {asg.state} -> {to_state} "
                f"not allowed")
        self.store.insert("ao_assignments", {
            "assignment_id": asg.assignment_id, "agent_id": asg.agent_id,
            "objective_json": json.dumps(asg.objective, sort_keys=True),
            "constraints_json": json.dumps(asg.constraints, sort_keys=True),
            "authority_scope_json": json.dumps(asg.authority_scope,
                                               sort_keys=True),
            "expected_outputs_json": json.dumps(asg.expected_outputs,
                                                sort_keys=True),
            "validation_requirements_json": json.dumps(
                asg.validation_requirements, sort_keys=True),
            "originating_decision": asg.originating_decision,
            "state": to_state, "created_at": asg.created_at})
        self.store.insert("ao_assignment_events", {
            "assignment_id": assignment_id, "from_state": asg.state,
            "to_state": to_state, "actor": actor, "reason": reason,
            "created_at": now()})
        return self.get(assignment_id)

    def create(self, agent_id: str, objective: Dict[str, Any],
               constraints: Dict[str, Any],
               authority_scope: Dict[str, Any],
               expected_outputs: Dict[str, Any],
               validation_requirements: Dict[str, Any],
               originating_decision: str,
               actor: str = "remor:engine") -> Assignment:
        agent = self.agents.get(agent_id)
        if agent.state != "AVAILABLE":
            raise AuthorityError(
                f"agent {agent_id} is {agent.state}, not AVAILABLE: "
                f"assignment refused")
        patterns = authority_scope.get("allowed_capability_patterns")
        if not isinstance(patterns, list) or not patterns:
            raise ValueError("authority_scope['allowed_capability_patterns'] "
                             "must be a non-empty list")
        raw = "|".join([agent_id, json.dumps(objective, sort_keys=True),
                        now(), patterns[0]])
        assignment_id = "asg_" + digest(raw)[:16]
        created = now()
        self.store.insert("ao_assignments", {
            "assignment_id": assignment_id, "agent_id": agent_id,
            "objective_json": json.dumps(objective, sort_keys=True),
            "constraints_json": json.dumps(constraints, sort_keys=True),
            "authority_scope_json": json.dumps(authority_scope, sort_keys=True),
            "expected_outputs_json": json.dumps(expected_outputs,
                                                 sort_keys=True),
            "validation_requirements_json": json.dumps(
                validation_requirements, sort_keys=True),
            "originating_decision": originating_decision,
            "state": CREATED, "created_at": created})
        self.store.insert("ao_assignment_events", {
            "assignment_id": assignment_id, "from_state": "",
            "to_state": CREATED, "actor": actor,
            "reason": f"created for agent {agent_id}", "created_at": created})
        self.agents.transition(agent_id, "ASSIGNED", actor=actor,
                               reason=f"assignment {assignment_id}")
        return self.get(assignment_id)

    def activate(self, assignment_id: str,
                 actor: str = "remor:engine") -> Assignment:
        asg = self._set_state(assignment_id, ACTIVE, actor,
                              reason="activated; issuing scoped grants")
        for pattern in asg.authority_scope["allowed_capability_patterns"]:
            grant_id = self.engine.issue_grant("capability:use", pattern,
                                               scope=assignment_id)
            self.store.insert("ao_assignment_grants", {
                "assignment_id": assignment_id, "grant_id": grant_id,
                "effect": "capability:use", "pattern": pattern,
                "created_at": now()})
        return self.get(assignment_id)

    def _close(self, assignment_id: str, to_state: str, actor: str,
               reason: str) -> Assignment:
        asg = self._set_state(assignment_id, to_state, actor, reason=reason)
        failures = []
        for row in self.store.rows("ao_assignment_grants", "assignment_id",
                                   assignment_id):
            try:
                self.engine.revoke_grant(row["grant_id"])
            except Exception as exc:
                # Attempt EVERY grant's revocation, but surface failures:
                # silently passing would let an unrevoked grant linger.
                failures.append((row["grant_id"], str(exc)))
        agent = self.agents.get(asg.agent_id)
        if agent.state == "ASSIGNED":
            self.agents.transition(agent.agent_id, "AVAILABLE", actor=actor,
                                   reason=f"assignment {assignment_id} "
                                          f"{to_state.lower()}")
        if failures:
            raise OrgError(
                f"assignment {assignment_id}: {len(failures)} grant "
                f"revocation(s) failed: {failures[:3]}")
        return asg

    def complete(self, assignment_id: str, actor: str = "remor:engine",
                 reason: str = "") -> Assignment:
        return self._close(assignment_id, COMPLETED, actor,
                           reason or "completed")

    def expire(self, assignment_id: str, actor: str = "remor:engine",
               reason: str = "") -> Assignment:
        return self._close(assignment_id, EXPIRED, actor,
                           reason or "expired")

    def revoke(self, assignment_id: str, actor: str = "remor:engine",
               reason: str = "") -> Assignment:
        return self._close(assignment_id, REVOKED, actor,
                           reason or "revoked")

    def require_active(self, assignment_id: str, agent_id: str) -> Assignment:
        asg = self.get(assignment_id)
        if asg.state != ACTIVE:
            raise AuthorityError(
                f"assignment {assignment_id} is {asg.state}, not ACTIVE: "
                f"mediated step refused")
        if asg.agent_id != agent_id:
            raise AuthorityError(
                f"assignment {assignment_id} belongs to agent "
                f"{asg.agent_id}, not {agent_id}: mediated step refused")
        return asg

    def grants_live(self, assignment_id: str) -> List[Dict[str, Any]]:
        return self.store.rows("ao_assignment_grants", "assignment_id",
                               assignment_id)
