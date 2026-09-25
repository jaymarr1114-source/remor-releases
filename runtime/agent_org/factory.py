"""Agent factory: template -> live agent instance.

Replication safety is structural, not conventional:
- fresh agent_id per instance (random component in the id derivation),
- fresh workspace directory per instance,
- fresh OracleRegistry producer (type "agent") per instance,
- NO grants are carried: grants are issued per-assignment only, by the
  AssignmentManager, scoped to the assignment id.

A template of substrate_kind "llm" with no explicit substrate raises
SubstrateUnavailable: the template exists, instantiation is honestly ABSENT.
"""
from __future__ import annotations

from typing import Any, Optional

from swarm_engine.agent_org.discovery import (
    make_discovery_callable,
    make_discovery_symbolic,
)
from swarm_engine.agent_org.exceptions import OrgError
from swarm_engine.agent_org.identity import AgentRecord
from swarm_engine.agent_org.registry import AgentRegistry
from swarm_engine.agent_org.substrates import Substrate, SubstrateUnavailable
from swarm_engine.agent_org.templates import AgentTemplate, TemplateRegistry


class AgentFactory:
    def __init__(self, registry: AgentRegistry,
                 templates: TemplateRegistry):
        self.registry = registry
        self.templates = templates

    def _default_substrate(self, template: AgentTemplate) -> Substrate:
        kind = template.substrate_kind
        if kind == "symbolic":
            return make_discovery_symbolic()
        if kind == "callable":
            return make_discovery_callable()
        if kind == "llm":
            raise SubstrateUnavailable(
                f"template {template.template_id}: substrate_kind 'llm' "
                f"has no instantiable substrate in this build (honest "
                f"ABSENT). Refusing rather than simulating.")
        raise OrgError(f"template {template.template_id}: unknown "
                       f"substrate_kind {kind!r}")

    def create(self, template_id: str, version: Optional[str] = None,
               substrate: Optional[Substrate] = None) -> AgentRecord:
        template = self.templates.get(template_id, version)
        if substrate is None:
            substrate = self._default_substrate(template)
        elif not isinstance(substrate, Substrate):
            raise TypeError("substrate must be a Substrate instance")
        record = self.registry.register(template, substrate)
        # Replication-safety assertion: nothing about this instance is
        # shared with any other (fresh id, fresh workspace, fresh producer).
        self.registry.transition(record.agent_id, "AVAILABLE",
                                 actor="remor:engine",
                                 reason=f"factory create from {template_id}")
        return self.registry.get(record.agent_id)
