"""
swarm_engine/core/arbitration.py

Replaces swarm_engine_v6's ModelRouter, which resolved every task to one
of three *external* providers (gpt-4o / claude-3-sonnet / local-llama-3)
keyed by cost-per-1k-tokens, and stamped the choice onto the task even
though nothing ever actually called those APIs. That design means "harder
tasks get handed to an outside paid model" — the opposite of a system
that's supposed to operate and manage itself.

LocalArbitrator replaces "which provider should handle this" with "what
does the engine itself need to do to handle this":

  DIRECT      - a matching capability/agent handler already exists, run now
  SYNTHESIZE  - no capability exists yet; CapabilitySynthesizer should
                write, verify, and register one (see synthesis/synthesizer.py)
  DECOMPOSE   - the task doesn't map to a single capability; it needs to be
                broken into subtasks (each of which gets arbitrated on its
                own) before anything executes

There is no external-provider concept anywhere in this file. Nothing here
makes a network call or references a paid API.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from swarm_engine.core.taskgraph import is_composite, split_composite


class ArbitrationTier(Enum):
    DIRECT = "direct"
    SYNTHESIZE = "synthesize"
    DECOMPOSE = "decompose"


@dataclass
class ArbitrationDecision:
    tier: ArbitrationTier
    task_type: str
    reason: str
    matched_capability: Optional[str] = None


class LocalArbitrator:
    """Decides how the engine should handle a task using only its own
    registered capabilities/agents and its own knowledge base — no
    external model selection, no provider cost tables."""

    # Heuristic: task types with more than this many ':' separated parts
    # (e.g. "forge:generate:critique:export") are treated as composite
    # and routed to DECOMPOSE rather than guessed at as a single capability.
    DECOMPOSITION_DEPTH_THRESHOLD = 2

    def __init__(self, agent_registry, knowledge_base=None, capability_store=None):
        self.agent_registry = agent_registry
        self.kb = knowledge_base
        self.store = capability_store

    def arbitrate(self, task_type: str, payload=None, composer=None, registry=None, examples=None, semantic_store=None) -> ArbitrationDecision:
        # An already-admitted capability wins over decomposition: if the whole
        # composite goal has been synthesized and proved before, re-splitting
        # it would throw away the capability the engine already earned.
        if self.store is not None:
            whole = self.store.resolve_goal(task_type)
            if whole is not None and whole.status == "active":
                return ArbitrationDecision(
                    tier=ArbitrationTier.DIRECT,
                    task_type=task_type,
                    reason=f"Admitted capability {whole.capability_id} already "
                           f"covers {task_type!r} in full.",
                    matched_capability=whole.capability_id,
                )
            # M+21: semantic/structural compatibility when exact goal_key misses
            try:
                compat = self.store.find_compatible(
                    task_type, payload=payload, composer=composer,
                    registry=registry, examples=examples,
                    semantic_store=semantic_store, min_score=0.55)
            except Exception:
                compat = []
            if compat and compat[0].status == "active":
                return ArbitrationDecision(
                    tier=ArbitrationTier.DIRECT,
                    task_type=task_type,
                    reason=(f"Compatible capability {compat[0].capability_id} "
                            f"matched {task_type!r} by structure/constraints "
                            f"({len(compat)} candidate(s))."),
                    matched_capability=compat[0].capability_id,
                )

        if is_composite(task_type):
            stages = split_composite(task_type)
            return ArbitrationDecision(
                tier=ArbitrationTier.DECOMPOSE,
                task_type=task_type,
                reason=f"Task type {task_type!r} describes {len(stages)} stages — "
                       f"decompose into a task graph before executing.",
            )

        matching_agents = self.agent_registry.find_by_capability(task_type)
        if matching_agents:
            return ArbitrationDecision(
                tier=ArbitrationTier.DIRECT,
                task_type=task_type,
                reason=f"{len(matching_agents)} agent(s) already registered for {task_type!r}.",
                matched_capability=task_type,
            )

        # A capability synthesized from primitives and admitted to the store is
        # a real handler, not a hint from history: the plan is on disk, its
        # dependencies were audited at boot, and it can be rehydrated and run.
        # Routing it DIRECT is what stops the engine resynthesizing something
        # it already built and proved.
        if self.store is not None:
            record = self.store.resolve_goal(task_type)
            if record is not None and record.status == "active":
                return ArbitrationDecision(
                    tier=ArbitrationTier.DIRECT,
                    task_type=task_type,
                    reason=f"Admitted capability {record.capability_id} handles "
                           f"{task_type!r} (success rate {record.success_rate:.0%}); "
                           f"rehydrate and execute it.",
                    matched_capability=record.capability_id,
                )

        # No agent currently has this capability. If we've synthesized it
        # before (tracked in the knowledge base), that's still DIRECT —
        # the capability just needs to be loaded, not rewritten.
        if self.kb is not None:
            rate = self.kb.body_plan_success_rate(task_type)
            if rate is not None:
                return ArbitrationDecision(
                    tier=ArbitrationTier.DIRECT,
                    task_type=task_type,
                    reason=f"Capability for {task_type!r} exists in knowledge base "
                           f"(historical success rate {rate:.0%}); reuse it.",
                    matched_capability=task_type,
                )

        return ArbitrationDecision(
            tier=ArbitrationTier.SYNTHESIZE,
            task_type=task_type,
            reason=f"No agent or stored capability handles {task_type!r} yet — "
                   f"engine must synthesize it locally.",
        )
