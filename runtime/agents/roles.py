"""
swarm_engine/agents/roles.py

Five specialized roles Agent 0 assigns work to.

Being honest about what these are: they are not five independent language
models. This environment has exactly one model — the one running this
conversation — and no access to spawn or call additional ones. Presenting
five agents as five separate intelligences would be a fabrication no
different from a hardcoded benchmark answer.

What is real: each role is a distinct orchestration lane, backed by a
different, already-proven engine subsystem, with its own scope of authority,
its own reliability record, and its own place in the assignment and
reconciliation logic. Agent 0 genuinely decides which lane handles a given
piece of work, genuinely tracks whether that lane is producing good results,
and genuinely reassigns work away from a lane that is failing. That is real
multi-role orchestration over one substrate, not five substrates.

  RESEARCHER  gap analysis, requirement decomposition, knowledge lookup
              -> CapabilityGapReasoner, KnowledgeStore
  BUILDER     synthesis and acquisition of missing capabilities
              -> AcquisitionOrchestrator, Planner/Composer
  VERIFIER    independent validation of anything the other roles produce
              -> IndependentValidator, VerificationPipeline
  DEBUGGER    failure diagnosis and recovery
              -> FailureDiagnoser, RecoveryEngine, FailureMemory
  OPTIMIZER   comparing and improving existing capabilities
              -> EvolutionEngine, CapabilityCompetition, SelfImprovementEngine
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class Role(Enum):
    RESEARCHER = "researcher"
    BUILDER = "builder"
    VERIFIER = "verifier"
    DEBUGGER = "debugger"
    OPTIMIZER = "optimizer"


@dataclass
class RoleOutcome:
    role: Role
    task: str
    success: bool
    value: Any = None
    detail: str = ""
    elapsed_ms: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"role": self.role.value, "task": self.task[:120],
                "success": self.success, "value": self.value,
                "detail": self.detail[:300], "elapsed_ms": round(self.elapsed_ms, 2)}


class SpecializedAgent:
    """One role's real handler, bound to the engine subsystem that actually
    does the work. `handle` is where the role's authority is enforced: a
    Researcher cannot register a capability, a Builder cannot declare a
    capability trustworthy — each role calls only the subsystem it owns."""

    def __init__(self, role: Role, engine, handler: Callable):
        self.role = role
        self.engine = engine
        self.handler = handler
        self.assignments = 0
        self.successes = 0

    @property
    def reliability(self) -> float:
        """Evidence-weighted, blended from a neutral prior of 0.5.

        Damping straight toward the observed rate (rate * evidence_weight)
        was the first version of this, and it was wrong: one success out of
        one gave 0.25, *below* an untouched role's neutral 0.5 — meaning a
        role that had just succeeded looked less trustworthy than one with no
        history at all. Blending from the prior fixes the direction: a
        success always moves reliability up from neutral, a failure always
        moves it down, and the move gets larger as evidence accumulates.
        """
        if self.assignments == 0:
            return 0.5
        rate = self.successes / self.assignments
        weight = self.assignments / (self.assignments + 3.0)
        return 0.5 * (1 - weight) + rate * weight

    async def handle(self, task: str, payload: Dict[str, Any]) -> RoleOutcome:
        started = time.time()
        self.assignments += 1
        try:
            value, detail = await self.handler(task, payload)
            outcome = RoleOutcome(self.role, task, True, value, detail,
                                  (time.time() - started) * 1000)
            self.successes += 1
            return outcome
        except Exception as exc:
            return RoleOutcome(self.role, task, False, None,
                              f"{type(exc).__name__}: {exc}",
                              (time.time() - started) * 1000)


def build_roles(engine) -> Dict[Role, SpecializedAgent]:
    """Wire each role to the real subsystem it is authoritative over."""

    async def researcher(task: str, payload: Dict[str, Any]):
        graph = engine.gap_reasoner.analyze(task,
                                            allow_effects=bool(payload.get("allow_effects")))
        return graph.as_dict(), f"{len(graph.gaps())} gap(s) of {len(graph.nodes)}"

    async def builder(task: str, payload: Dict[str, Any]):
        # The Builder's entire job is producing a working capability, so its
        # RoleOutcome.success must mean exactly that -- not "the Python call
        # returned without raising". Returning a structured {"acquired":
        # False, ...} without raising let a failed acquisition report as a
        # successful RoleOutcome, which let the project loop mark a
        # requirement satisfied when nothing was ever built or registered.
        # Raising here is what makes success mean what it says.
        examples = payload.get("examples")
        if examples:
            result = await engine.acquire_capability(
                payload.get("name", task), payload.get("description", task),
                examples=list(examples))
            if not result.get("acquired"):
                raise RuntimeError(result.get("reason", "acquisition failed"))
            return result, "acquired"
        result = await engine.acquisition_orchestrator.resolve(
            task, examples_by_node=payload.get("examples_by_node"),
            allow_effects=bool(payload.get("allow_effects")))
        if not result.fully_resolved:
            raise RuntimeError(f"could not resolve: {result.failed}")
        return result.as_dict(), "resolved"

    async def verifier(task: str, payload: Dict[str, Any]):
        from swarm_engine.verification.independent import IndependentValidator
        from swarm_engine.acquisition.semantic import Case
        code = payload["code"]
        entrypoint = payload.get("entrypoint", "capability")
        spec = payload["spec"]
        cases = [Case(**c) for c in payload.get("cases", [])]
        validator = IndependentValidator(
            engine.acquisition.process_sandbox.run,
            oracle_registry=getattr(engine, "oracle_registry", None),
            engine_oracle=getattr(engine, "oracle", None))
        verdict = validator.validate(code, entrypoint, spec, cases)
        return verdict.as_dict(), ("admitted" if verdict.admitted
                                   else "; ".join(verdict.reasons)[:200])

    async def debugger(task: str, payload: Dict[str, Any]):
        error = payload.get("error", "")
        diagnosis = engine.diagnoser.diagnose(error)
        record = engine.failure_memory.record(task, error)
        recovered, value, attempts = await engine.recovery.recover(
            task, payload.get("args", {}), error)
        return ({"diagnosis": diagnosis.as_dict(), "recovered": recovered,
                "value": value, "attempts": [a.detail for a in attempts]},
               diagnosis.kind.value)

    async def optimizer(task: str, payload: Dict[str, Any]):
        cases = payload.get("cases", [])
        if payload.get("tournament") and cases:
            result = await engine.competition.tournament(task, cases)
            return result, f"{len(result.get('survivors', []))} survivor(s)"
        result = await engine.evolution.evolve(task, cases)
        return result.as_dict(), result.reason

    handlers = {Role.RESEARCHER: researcher, Role.BUILDER: builder,
               Role.VERIFIER: verifier, Role.DEBUGGER: debugger,
               Role.OPTIMIZER: optimizer}
    return {role: SpecializedAgent(role, engine, fn) for role, fn in handlers.items()}
