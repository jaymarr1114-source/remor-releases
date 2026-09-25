"""Agent templates: versioned, engine-authorized blueprints.

A template pins the contracts an agent instance must satisfy but never the
instance's identity, substrate instance, or history. Registration is
engine-authorized and recorded as a chained event.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.agent_org.exceptions import AuthorityError
from swarm_engine.agent_org.store import OrgStore, now

try:
    from swarm_engine.governance.oracle_binding import EngineOracleHandle
except Exception:  # pragma: no cover - import-time safety only
    EngineOracleHandle = None  # type: ignore

CONTRACT_KEYS = (
    "reasoning_interface",
    "tool_interface",
    "capability_requirements",
    "execution_contract",
    "io_contract",
    "evidence_contract",
    "workspace_requirements",
    "resource_requirements",
    "persistence_requirements",
    "provenance_requirements",
    "verification_requirements",
)


@dataclass
class AgentTemplate:
    template_id: str
    family: str
    version: str
    substrate_kind: str  # "llm" | "symbolic" | "callable"
    contracts: Dict[str, Any] = field(default_factory=dict)


def _require_engine(engine: Any) -> None:
    if EngineOracleHandle is None or not isinstance(engine, EngineOracleHandle):
        raise AuthorityError(
            "template registration requires a live EngineOracleHandle")
    if engine.producer_id != "remor:engine":
        raise AuthorityError(
            f"template registration requires producer 'remor:engine', "
            f"got {engine.producer_id!r}")


class TemplateRegistry:
    def __init__(self, store: OrgStore, engine: Any):
        self.store = store
        self.engine = engine

    def register_template(self, template_id: str, family: str, version: str,
                          substrate_kind: str, contracts: Dict[str, Any],
                          actor: str = "remor:engine") -> AgentTemplate:
        _require_engine(self.engine)
        if substrate_kind not in ("llm", "symbolic", "callable"):
            raise ValueError(f"unknown substrate_kind {substrate_kind!r}")
        missing = [k for k in CONTRACT_KEYS if k not in contracts]
        if missing:
            raise ValueError(f"template {template_id}: missing contracts {missing}")
        if self.store.latest("ao_templates", "template_id", template_id):
            row = self.store.latest("ao_templates", "template_id", template_id)
            if row and row.get("version") == version:
                raise ValueError(
                    f"template {template_id} version {version} already registered")
        self.store.insert("ao_templates", {
            "template_id": template_id, "version": version, "family": family,
            "substrate_kind": substrate_kind,
            "contracts_json": json.dumps(contracts, sort_keys=True),
            "created_at": now()})
        self.store.insert("ao_template_events", {
            "template_id": template_id, "version": version, "actor": actor,
            "action": "registered", "created_at": now()})
        return AgentTemplate(template_id, family, version, substrate_kind,
                             dict(contracts))

    def get(self, template_id: str,
            version: Optional[str] = None) -> AgentTemplate:
        rows = self.store.rows("ao_templates", "template_id", template_id)
        if not rows:
            raise KeyError(f"unknown template {template_id!r}")
        if version is not None:
            rows = [r for r in rows if r["version"] == version]
            if not rows:
                raise KeyError(
                    f"unknown template {template_id!r} version {version!r}")
        row = rows[-1]
        return AgentTemplate(
            row["template_id"], row["family"], row["version"],
            row["substrate_kind"], json.loads(row["contracts_json"]))

    def list(self) -> List[AgentTemplate]:
        seen: Dict[str, AgentTemplate] = {}
        for row in self.store.rows("ao_templates"):
            seen[row["template_id"]] = AgentTemplate(
                row["template_id"], row["family"], row["version"],
                row["substrate_kind"], json.loads(row["contracts_json"]))
        return list(seen.values())


def seed_templates(registry: TemplateRegistry) -> List[AgentTemplate]:
    """Seed the three demo templates. Idempotent: skips existing versions."""
    base_contracts = {
        "reasoning_interface": "task dict in (JSON-serializable), result dict out",
        "tool_interface": "none: substrate-internal execution only",
        "capability_requirements": ["codec:discovery"],
        "execution_contract": "deterministic given task; no network; no side effects",
        "io_contract": {"input": "bytes", "output": "bytes"},
        "evidence_contract": "result.measurements must contain per-trial ratios",
        "workspace_requirements": "isolated agent workspace dir, no shared state",
        "resource_requirements": {"max_seconds": 120, "max_memory_mb": 512},
        "persistence_requirements": "no substrate-persisted state across assignments",
        "provenance_requirements": "supplier_id = agent producer on every evaluation",
        "verification_requirements": "independent subprocess verification before admission",
    }

    def contracts(kind: str, note: str) -> Dict[str, Any]:
        c = dict(base_contracts)
        c["reasoning_interface"] = note
        return c

    seeds = [
        ("tpl_llm_coder_v1", "coder", "1", "llm",
         contracts("llm", "LLM reasoning interface (ABSENT: instantiation refused)")),
        ("tpl_symbolic_coder_v1", "coder", "1", "symbolic",
         contracts("symbolic", "ordered predicate->emitter rules; first match wins")),
        ("tpl_callable_coder_v1", "coder", "1", "callable",
         contracts("callable", "exhaustive grid search callable")),
    ]
    out = []
    for tid, family, version, kind, c in seeds:
        try:
            registry.get(tid, version)
        except KeyError:
            out.append(registry.register_template(tid, family, version, kind, c))
    return out
