"""REMOR Agent Organization & Persistent Learning package.

Implements the agent-organization substrate: templates, agent lifecycle,
assignments with scoped authority, mediated execution, independent review,
experience (L1 candidate -> L2 verified -> L3 synthesized), and measured
synthesis. All state transitions are validated; all refusals raise.

Lineage: producing agent Grok (xAI), bench Felix.
Anti-simulation: no boundary counts as implemented because a class/table
exists -- every state transition is validated, every refusal raises.
"""
from swarm_engine.agent_org.exceptions import (
    OrgError,
    AuthorityError,
    LifecycleError,
    ExecutionError,
    VerificationFailed,
    SynthesisError,
)
from swarm_engine.agent_org.substrates import (
    Substrate,
    SubstrateUnavailable,
    CallableSubstrate,
    SymbolicSubstrate,
    LLMSubstrate,
)
from swarm_engine.agent_org.store import OrgStore
from swarm_engine.agent_org.identity import AgentRecord
from swarm_engine.agent_org.registry import AgentRegistry
from swarm_engine.agent_org.factory import AgentFactory
from swarm_engine.agent_org.templates import AgentTemplate, TemplateRegistry
from swarm_engine.agent_org.assignment import Assignment, AssignmentManager
from swarm_engine.agent_org.work_product import WorkProduct
from swarm_engine.agent_org.runner import AgentRunner
from swarm_engine.agent_org.review import ReviewBoard
from swarm_engine.agent_org.experience import (
    ExperienceCandidate,
    OrganizationalExperience,
    ExperienceStore,
)
from swarm_engine.agent_org.performance import PerformanceLog
from swarm_engine.agent_org.synthesis import (
    RelationshipFinder,
    Relationship,
    OrgSynthesizer,
    emit_fused_codec,
    MIXED_CORPUS,
    tokenize,
    token_similarity,
)
from swarm_engine.agent_org.subprocess_runner import run_code, RunReport
from swarm_engine.agent_org.org import RemorOrganization

__all__ = [
    "OrgError",
    "AuthorityError",
    "LifecycleError",
    "ExecutionError",
    "VerificationFailed",
    "SynthesisError",
    "Substrate",
    "SubstrateUnavailable",
    "CallableSubstrate",
    "SymbolicSubstrate",
    "LLMSubstrate",
    "OrgStore",
    "AgentRecord",
    "AgentRegistry",
    "AgentFactory",
    "AgentTemplate",
    "TemplateRegistry",
    "Assignment",
    "AssignmentManager",
    "WorkProduct",
    "AgentRunner",
    "ReviewBoard",
    "ExperienceCandidate",
    "OrganizationalExperience",
    "ExperienceStore",
    "PerformanceLog",
    "RelationshipFinder",
    "Relationship",
    "OrgSynthesizer",
    "emit_fused_codec",
    "MIXED_CORPUS",
    "tokenize",
    "token_similarity",
    "run_code",
    "RunReport",
    "RemorOrganization",
]
