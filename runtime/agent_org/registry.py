"""Agent registry: identity, lifecycle, substrate replacement, destruction.

Lifecycle (every pair not listed raises LifecycleError):
    REGISTERED  -> AVAILABLE
    AVAILABLE   -> ASSIGNED, RETIRED
    ASSIGNED    -> EXECUTING, AVAILABLE, RETIRED
    EXECUTING   -> SUBMITTED, FAILED, INTERRUPTED
    SUBMITTED   -> UNDER_REVIEW
    UNDER_REVIEW-> ACCEPTED, REJECTED, FAILED
    ACCEPTED    -> AVAILABLE, RETIRED
    REJECTED    -> AVAILABLE, RETIRED
    FAILED      -> RECOVERING, RETIRED
    INTERRUPTED -> RECOVERING, RETIRED
    RECOVERING  -> AVAILABLE, RETIRED
    QUARANTINED -> RETIRED
    RETIRED     -> (terminal)
    DESTROYED   -> (terminal)

destroy() may be called from any non-terminal state; it is the only way to
reach DESTROYED. replace_substrate() is allowed only in AVAILABLE/RETIRED
and preserves identity, history, assignments, and producer.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from typing import Any, Dict, List, Optional

from swarm_engine.agent_org.exceptions import AuthorityError, LifecycleError
from swarm_engine.agent_org.identity import AgentRecord
from swarm_engine.agent_org.store import OrgStore, digest, now
from swarm_engine.agent_org.substrates import Substrate
from swarm_engine.agent_org.templates import AgentTemplate

ALLOWED: Dict[str, tuple] = {
    "REGISTERED": ("AVAILABLE",),
    "AVAILABLE": ("ASSIGNED", "RETIRED"),
    "ASSIGNED": ("EXECUTING", "AVAILABLE", "RETIRED"),
    "EXECUTING": ("SUBMITTED", "FAILED", "INTERRUPTED"),
    "SUBMITTED": ("UNDER_REVIEW",),
    "UNDER_REVIEW": ("ACCEPTED", "REJECTED", "FAILED"),
    "ACCEPTED": ("AVAILABLE", "RETIRED"),
    "REJECTED": ("AVAILABLE", "RETIRED"),
    "FAILED": ("RECOVERING", "RETIRED"),
    "INTERRUPTED": ("RECOVERING", "RETIRED"),
    "RECOVERING": ("AVAILABLE", "RETIRED"),
    "QUARANTINED": ("RETIRED",),
    "RETIRED": (),
    "DESTROYED": (),
}

TERMINAL = ("RETIRED", "DESTROYED")


class AgentRegistry:
    def __init__(self, store: OrgStore, oregistry: Any, engine: Any,
                 agents_root: str):
        self.store = store
        self.oregistry = oregistry
        self.engine = engine
        self.agents_root = agents_root
        os.makedirs(agents_root, exist_ok=True)
        # In-memory only: substrate instances and producer custody tokens.
        # Substrates never see tokens; the runner holds custody.
        self._substrates: Dict[str, Substrate] = {}
        self._custody_tokens: Dict[str, str] = {}

    # -- identity -------------------------------------------------------
    @staticmethod
    def _new_agent_id(template_id: str, version: str) -> str:
        raw = "|".join([template_id, version, secrets.token_hex(16)])
        return "agt_" + digest(raw)[:16]

    def register(self, template: AgentTemplate, substrate: Substrate,
                 actor: str = "remor:engine") -> AgentRecord:
        if not isinstance(substrate, Substrate):
            raise TypeError("substrate must be a Substrate instance")
        agent_id = self._new_agent_id(template.template_id, template.version)
        while agent_id in self._substrates:  # paranoia: never collide
            agent_id = self._new_agent_id(template.template_id, template.version)
        credential = self.oregistry.register_producer(
            "agent", source=f"agent:{agent_id}")
        workspace = os.path.join(self.agents_root, agent_id)
        os.makedirs(workspace, exist_ok=False)
        created = now()
        self.store.insert("ao_agents", {
            "agent_id": agent_id, "template_id": template.template_id,
            "template_version": template.version,
            "substrate_id": substrate.substrate_id,
            "substrate_kind": substrate.kind,
            "producer_id": credential.producer_id, "state": "REGISTERED",
            "workspace_path": workspace, "created_at": created})
        self.store.insert("ao_agent_events", {
            "agent_id": agent_id, "from_state": "", "to_state": "REGISTERED",
            "actor": actor, "reason": "registered",
            "substrate_old": "", "substrate_new": substrate.substrate_id,
            "created_at": created})
        self._substrates[agent_id] = substrate
        self._custody_tokens[agent_id] = credential.token
        return self.get(agent_id)

    # -- reads ----------------------------------------------------------
    def _row_to_record(self, row: Dict[str, Any]) -> AgentRecord:
        return AgentRecord(
            agent_id=row["agent_id"], template_id=row["template_id"],
            template_version=row["template_version"],
            substrate_id=row["substrate_id"],
            substrate_kind=row["substrate_kind"],
            producer_id=row["producer_id"], state=row["state"],
            workspace_path=row["workspace_path"], created_at=row["created_at"])

    def get(self, agent_id: str) -> AgentRecord:
        row = self.store.latest("ao_agents", "agent_id", agent_id)
        if row is None:
            raise KeyError(f"unknown agent {agent_id!r}")
        return self._row_to_record(row)

    def list(self, state: Optional[str] = None) -> List[AgentRecord]:
        rows = self.store.rows("ao_agents")
        latest: Dict[str, Dict] = {}
        for row in rows:
            latest[row["agent_id"]] = row
        recs = [self._row_to_record(r) for r in latest.values()]
        if state is not None:
            recs = [r for r in recs if r.state == state]
        return recs

    def get_substrate(self, agent_id: str) -> Substrate:
        try:
            return self._substrates[agent_id]
        except KeyError:
            raise KeyError(f"no live substrate for agent {agent_id!r} "
                           f"(destroyed or unknown)")

    def custody_token(self, agent_id: str) -> str:
        """REMOR-internal: the producer token for custody records.

        Held in memory only; never written to sqlite. Substrates never see
        it -- the runner is the custodian.
        """
        try:
            return self._custody_tokens[agent_id]
        except KeyError:
            raise AuthorityError(
                f"no custody token for agent {agent_id!r}")

    # -- lifecycle ------------------------------------------------------
    def transition(self, agent_id: str, to_state: str, actor: str,
                   reason: str = "") -> AgentRecord:
        rec = self.get(agent_id)
        allowed = ALLOWED.get(rec.state, ())
        if to_state not in allowed:
            raise LifecycleError(
                f"agent {agent_id}: transition {rec.state} -> {to_state} "
                f"not allowed (allowed: {list(allowed) or 'none'})")
        self.store.insert("ao_agents", {
            "agent_id": agent_id, "template_id": rec.template_id,
            "template_version": rec.template_version,
            "substrate_id": rec.substrate_id,
            "substrate_kind": rec.substrate_kind,
            "producer_id": rec.producer_id, "state": to_state,
            "workspace_path": rec.workspace_path,
            "created_at": rec.created_at})
        self.store.insert("ao_agent_events", {
            "agent_id": agent_id, "from_state": rec.state,
            "to_state": to_state, "actor": actor, "reason": reason,
            "substrate_old": "", "substrate_new": "", "created_at": now()})
        return self.get(agent_id)

    def destroy(self, agent_id: str, actor: str, reason: str = "") -> AgentRecord:
        """Terminal destruction: workspace removed, custody dropped.

        History rows remain. Experience rows are never cascaded.
        """
        rec = self.get(agent_id)
        if rec.state == "DESTROYED":
            raise LifecycleError(f"agent {agent_id} already DESTROYED")
        self.store.insert("ao_agents", {
            "agent_id": agent_id, "template_id": rec.template_id,
            "template_version": rec.template_version,
            "substrate_id": rec.substrate_id,
            "substrate_kind": rec.substrate_kind,
            "producer_id": rec.producer_id, "state": "DESTROYED",
            "workspace_path": rec.workspace_path,
            "created_at": rec.created_at})
        self.store.insert("ao_agent_events", {
            "agent_id": agent_id, "from_state": rec.state,
            "to_state": "DESTROYED", "actor": actor,
            "reason": reason or "destroyed",
            "substrate_old": "", "substrate_new": "", "created_at": now()})
        shutil.rmtree(rec.workspace_path, ignore_errors=True)
        self._substrates.pop(agent_id, None)
        self._custody_tokens.pop(agent_id, None)
        return self.get(agent_id)

    def replace_substrate(self, agent_id: str, new_substrate: Substrate,
                          actor: str, reason: str = "") -> AgentRecord:
        """Model-substitution: swap the substrate, keep everything else.

        Allowed only in AVAILABLE or RETIRED. Preserves agent_id, history,
        assignments, and producer. A kind incompatible with the template is
        allowed (that is the point of substrate independence) but recorded.
        """
        if not isinstance(new_substrate, Substrate):
            raise TypeError("new_substrate must be a Substrate instance")
        rec = self.get(agent_id)
        if rec.state not in ("AVAILABLE", "RETIRED"):
            raise LifecycleError(
                f"agent {agent_id}: replace_substrate allowed only in "
                f"AVAILABLE/RETIRED, not {rec.state}")
        old_id = rec.substrate_id
        new_id = new_substrate.substrate_id
        self.store.insert("ao_agents", {
            "agent_id": agent_id, "template_id": rec.template_id,
            "template_version": rec.template_version,
            "substrate_id": new_id, "substrate_kind": new_substrate.kind,
            "producer_id": rec.producer_id, "state": rec.state,
            "workspace_path": rec.workspace_path,
            "created_at": rec.created_at})
        self.store.insert("ao_agent_events", {
            "agent_id": agent_id, "from_state": rec.state,
            "to_state": rec.state, "actor": actor,
            "reason": reason or "substrate replaced",
            "substrate_old": old_id, "substrate_new": new_id,
            "created_at": now()})
        self._substrates[agent_id] = new_substrate
        return self.get(agent_id)
