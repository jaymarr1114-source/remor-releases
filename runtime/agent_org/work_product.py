"""Work products: content-addressed outputs of agent execution.

A WorkProduct is immutable content (artifacts + outputs) plus a mutable
lifecycle state. self_reported_success is STORED but NEVER consulted by any
review path -- the review machinery is forbidden from reading it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.agent_org.store import OrgStore

DRAFT, SUBMITTED, UNDER_REVIEW, ACCEPTED, REJECTED = (
    "DRAFT", "SUBMITTED", "UNDER_REVIEW", "ACCEPTED", "REJECTED")


@dataclass
class WorkProduct:
    wp_id: str
    assignment_id: str
    agent_id: str
    template_id: str
    template_version: str
    substrate_id: str
    entrypoint: str
    input_digest: str
    outputs: Dict[str, Any]
    artifacts: List[Dict[str, Any]]
    evidence_refs: Dict[str, Any]
    environment: Dict[str, Any]
    self_reported_success: Any  # stored only; review must never read this
    state: str
    created_at: str


def row_to_work_product(row: Dict[str, Any]) -> WorkProduct:
    return WorkProduct(
        wp_id=row["wp_id"], assignment_id=row["assignment_id"],
        agent_id=row["agent_id"], template_id=row["template_id"],
        template_version=row["template_version"],
        substrate_id=row["substrate_id"], entrypoint=row["entrypoint"],
        input_digest=row["input_digest"],
        outputs=json.loads(row["outputs_json"]),
        artifacts=json.loads(row["artifacts_json"]),
        evidence_refs=json.loads(row["evidence_refs_json"]),
        environment=json.loads(row["environment_json"]),
        self_reported_success=json.loads(row["self_reported_success_json"]),
        state=row["state"], created_at=row["created_at"])


def persist_work_product(store: OrgStore, wp: WorkProduct) -> None:
    store.insert("ao_work_products", {
        "wp_id": wp.wp_id, "assignment_id": wp.assignment_id,
        "agent_id": wp.agent_id, "template_id": wp.template_id,
        "template_version": wp.template_version,
        "substrate_id": wp.substrate_id, "entrypoint": wp.entrypoint,
        "input_digest": wp.input_digest,
        "outputs_json": json.dumps(wp.outputs, sort_keys=True, default=str),
        "artifacts_json": json.dumps(wp.artifacts, sort_keys=True,
                                     default=str),
        "evidence_refs_json": json.dumps(wp.evidence_refs, sort_keys=True,
                                         default=str),
        "environment_json": json.dumps(wp.environment, sort_keys=True,
                                       default=str),
        "self_reported_success_json": json.dumps(wp.self_reported_success,
                                                 default=str),
        "state": wp.state, "created_at": wp.created_at})


def get_work_product(store: OrgStore, wp_id: str) -> WorkProduct:
    row = store.latest("ao_work_products", "wp_id", wp_id)
    if row is None:
        raise KeyError(f"unknown work product {wp_id!r}")
    return row_to_work_product(row)
