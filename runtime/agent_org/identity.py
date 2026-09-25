"""Agent identity records.

An agent's identity is: template pins + substrate id + a fresh random
component. The agent_id is content-derived so replication accidents are
structurally impossible: two registrations never share an id.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class AgentRecord:
    agent_id: str
    template_id: str
    template_version: str
    substrate_id: str
    substrate_kind: str
    producer_id: str
    state: str
    workspace_path: str
    created_at: str

    @property
    def template_pin(self) -> str:
        return f"{self.template_id}@{self.template_version}"
