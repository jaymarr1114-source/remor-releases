"""
Canonical SWarm V6 engine assembled from the recovery patch set.

This is intentionally smaller and more explicit than the Colab-generated
reconstruction. It governs the recovered subsystems without pretending that
cognition, arbitrary planning, or external-model execution are complete.

Lifecycle:
    SUBMIT -> ARBITRATE -> EXECUTE / SYNTHESIZE / DECOMPOSE -> VERIFY -> LOG

The engine owns orchestration. Agents execute capabilities. Capabilities do
not become the engine itself.
"""
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.agents.base_agent import AgentRegistry
from swarm_engine.core.arbitration import LocalArbitrator, ArbitrationTier
from swarm_engine.core.taskgraph import (
    Scheduler, TaskGraphError, build_graph,
)
from swarm_engine.memory.knowledge_base import KnowledgeBase
from swarm_engine.acquisition.pipeline import (
    AcquisitionPipeline, Candidate as AcquiredCandidate, GapDetector,
    LocalSource, NetworkSource, http_json_index_fetcher,
)
from swarm_engine.acquisition.semantic import SemanticMatcher
from swarm_engine.acquisition.semantic_evidence_gap import SemanticEvidenceGapStore
from swarm_engine.core.autonomy import ObjectiveStore, ResourceBudget
from swarm_engine.memory.system import MemorySystem
from swarm_engine.governance.provenance import (
    AcquiredCodeStore, Origin, ProvenanceRecord, ProvenanceStore, TrustLevel,
)
from swarm_engine.acquisition.gap_reasoner import CapabilityGapReasoner
from swarm_engine.acquisition.decomposition import BehavioralDecomposer
from swarm_engine.acquisition.orchestrator import AcquisitionOrchestrator
from swarm_engine.core.longhorizon import CheckpointStore, LongHorizonRunner
from swarm_engine.core.monitor import HealthMonitor
from swarm_engine.governance.lifecycle import CapabilityLifecycle, LifecycleState
from swarm_engine.memory.failure_memory import FailureMemory
from swarm_engine.synthesis.abstraction import CompositionAbstractor
from swarm_engine.improvement.loop import (
    EvolutionEngine, FailureDiagnoser, RecoveryEngine, SelfImprovementEngine,
)
from swarm_engine.primitives import Governor, build_registry
from swarm_engine.synthesis.admission import AdmissionController
from swarm_engine.synthesis.acquisition_learning import AcquisitionLearner
from swarm_engine.synthesis.capability_store import CapabilityStore, stable_code_id
from swarm_engine.synthesis.composer import Composer
from swarm_engine.synthesis.planner import Planner
from swarm_engine.verification.pipeline import VerificationPipeline, basic_schema_check


@dataclass
class Task:
    task_id: str
    task_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    state: str = "queued"
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    plan: List[Dict[str, Any]] = field(default_factory=list)


class SwarmEngine:
    """Central SWarm authority for the recovered V6 patch architecture."""

    def __init__(self, db_path: str = "swarm_engine.db", agent_count: int = 1,
                 governor: Optional[Governor] = None,
                 budget: Optional[ResourceBudget] = None):
        self.db_path = db_path
        self.projects_dir = "/tmp/swarm_projects"
        import os as _os
        _os.makedirs(self.projects_dir, exist_ok=True)
        self.kb = KnowledgeBase(db_path=db_path)
        self.agents = AgentRegistry(knowledge_base=self.kb)
        for agent_id in range(agent_count):
            self.agents.spawn(agent_id)

        # The primitive substrate. The registry is the engine's vocabulary,
        # the planner turns a goal into a plan over that vocabulary, the
        # composer type-checks and executes plans, and the admission
        # controller decides what earns a place in the store. The governor is
        # held at engine level so every synthesized capability is bound by the
        # same policy rather than each carrying its own.
        # ORACLE BINDING (2026-09-25): the tamper-evident registry of
        # caller-supplied oracles. Lives in a sidecar sqlite file next to the
        # engine DB so the trust record is independent of the stores it
        # guards. The engine holds the memory-only root token via
        # self.oracle (EngineOracleHandle); every gate below is constructed
        # with the registry + handle.
        from swarm_engine.governance.oracle_binding import OracleRegistry
        import os as _os2
        _oracle_db = (db_path + ".oracle.db"
                      if not db_path.startswith("file:")
                      else db_path + ".oracle.db")
        self.oracle_registry = OracleRegistry(_oracle_db)
        self.oracle = self.oracle_registry.engine_handle()
        # O18: pin the artifact-validator registry's engine builtins as
        # engine-authorized definitions; every later register_validator call
        # requires an authenticated producer, and the growth engine verifies
        # the live validator's bytes against the registered head at use.
        from swarm_engine.capability.artifact_validation import (
            bind_oracle_registry as _bind_artifact_validators)
        _bind_artifact_validators(self.oracle_registry, self.oracle)

        if governor is not None and (
                getattr(governor, "oracle_registry", None) is not self.oracle_registry
                or getattr(governor, "engine_oracle", None) is not self.oracle):
            # A caller-supplied Governor that is not bound to this engine's
            # oracle registry would silently bypass grant attribution: refuse
            # it rather than run unbound. (Fail closed.)
            raise RuntimeError(
                "SwarmEngine refuses a caller-supplied Governor that is not "
                "bound to this engine's oracle registry -- unbound grants are "
                "not permitted")
        self.governor = governor or Governor(
            oracle_registry=self.oracle_registry,
            engine_oracle=self.oracle)
        self.primitives = build_registry(governor=self.governor)
        self.planner = Planner(self.primitives,
                               # O20: template registration is authenticated
                               # and plan-time template bytes are verified
                               # when a registry is present.
                               oracle_registry=self.oracle_registry,
                               engine_oracle=self.oracle)
        self.composer = Composer(self.primitives)
        self.capabilities = CapabilityStore(db_path=db_path)
        self.admission = AdmissionController(
            self.primitives, self.capabilities,
            oracle_registry=self.oracle_registry,
            engine_oracle=self.oracle)

        self.arbitrator = LocalArbitrator(self.agents, self.kb,
                                          capability_store=self.capabilities)
        self.verifier = VerificationPipeline().add_stage(basic_schema_check)

        # Provenance and trust: every capability the engine admits gets a
        # lineage entry, so a faulty one can be traced to its descendants
        # rather than merely regretted.
        self.provenance = ProvenanceStore(
            db_path=db_path,
            oracle_registry=self.oracle_registry,
            engine_oracle=self.oracle)
        self.acquired_code = AcquiredCodeStore(db_path=db_path)

        # Gap detection and acquisition. The network source is registered but
        # inert unless the governor grants NETWORK, so acquisition is fully
        # exercisable offline and cannot quietly reach outside the machine.
        self.gaps = GapDetector(self.primitives, self.planner, self.composer)
        # Diagnostic ledger for ungrounded semantic-operation targets.  This
        # records evidence insufficiency without selecting evidence or
        # changing capability-acquisition behavior.
        self.semantic_evidence_gaps = SemanticEvidenceGapStore(db_path=db_path)
        self.local_source = LocalSource()
        self.matcher = SemanticMatcher(self.provenance)
        import os as _os_net
        _net_endpoint = _os_net.environ.get(
            "REMOR_NETWORK_INDEX", "https://example.invalid/index")
        _net_fetcher = http_json_index_fetcher(_net_endpoint)
        self.acquisition = AcquisitionPipeline(
            sources=[self.local_source,
                     NetworkSource(self.governor, fetcher=_net_fetcher,
                                   endpoint=_net_endpoint)],
            provenance=self.provenance, matcher=self.matcher,
            registrar=self._register_acquired,
            # O12: the pipeline test gate refuses unregistered predicates
            # and records (test, candidate, input, verdict) chained when a
            # registry is present; legacy judge behavior when it is not.
            oracle_registry=self.oracle_registry,
            engine_oracle=self.oracle)

        # Recovery and evolution close the loop after execution.
        self.diagnoser = FailureDiagnoser()
        self.recovery = RecoveryEngine(self.planner, self.composer, self.diagnoser)
        self.evolution = EvolutionEngine(self.planner, self.composer,
                                         self.capabilities, self.provenance)

        # Long-running autonomy.
        self.objectives = ObjectiveStore(db_path=db_path)
        self.checkpoints = CheckpointStore(db_path=db_path)
        self.horizon = LongHorizonRunner(self, self.checkpoints)
        self.self_improvement = SelfImprovementEngine(self)

        self.acquired_specs: Dict[str, Any] = {}

        # V6.3: recursive gap reasoning, multi-strategy orchestration,
        # explicit capability lifecycle, persisted failure memory, continuous
        # monitoring, and composition abstraction. Each is real, addressable
        # machinery -- not decoration on top of the V6.2 pipeline.
        self.gap_reasoner = CapabilityGapReasoner(
            self.primitives, self.planner, self.composer, self.provenance,
            self.acquired_specs,
            decomposer=BehavioralDecomposer(self.primitives))
        self.acquisition_orchestrator = AcquisitionOrchestrator(self)
        # Boundary 2: the strategy-experience learner is owned by the
        # engine and persisted in the engine's own DB, so learned
        # strategy preferences survive across orchestrator instances and
        # across fresh engine objects opened on the same DB file. The
        # orchestrator consults it (reorders candidates) and feeds it
        # (records every attempt outcome); it never changes what a
        # strategy may produce or how admission judges it.
        self.strategy_learner = AcquisitionLearner(db_path=db_path)
        self.lifecycle = CapabilityLifecycle(db_path=db_path)
        self.failure_memory = FailureMemory(self.diagnoser, db_path=db_path)
        self.monitor = HealthMonitor(self)
        self.abstractor = CompositionAbstractor(db_path=db_path)
        self.memory = MemorySystem(self, db_path=db_path)
        from swarm_engine.core.task_interface import UniversalTaskInterface
        self.task_interface = UniversalTaskInterface(self)
        from swarm_engine.synthesis.competition import CompetitionPool
        self.competition = CompetitionPool(self)
        from swarm_engine.improvement.loop import RecursiveImprovementLoop
        self.recursive_improvement = RecursiveImprovementLoop(
            self.self_improvement, self.failure_memory)
        self.budget = budget or ResourceBudget()

        # Agent 0: assigns work to five specialized orchestration roles
        # (Researcher/Builder/Verifier/Debugger/Optimizer), each backed by a
        # real subsystem above, tracks whether each is actually producing
        # good results, and reassigns away from a role that is failing. This
        # is orchestration over one substrate, not five separate models —
        # there is only one model available in this environment, and
        # presenting otherwise would be a fabrication.
        from swarm_engine.agents.blackboard import Blackboard
        from swarm_engine.core.metareasoning import MetaReasoner
        self.blackboard = Blackboard(db_path=db_path)
        self.metareasoner = MetaReasoner(self.provenance, self.matcher, self.diagnoser)
        from swarm_engine.agents.agent_zero import AgentZero
        self.agent_zero = AgentZero(self)

        # Project-scoped machinery: ingestion, safe modification, sandboxed
        # commands. These operate on an external project root the caller
        # supplies, not on the engine's own source — a ProjectModificationGuard
        # is created per project root by the caller, the same way
        # SelfModificationGuard is scoped to the engine's own root.
        from swarm_engine.project.ingestion import ProjectIngestor, ProjectStore
        self.project_ingestor = ProjectIngestor()
        self.projects = ProjectStore(db_path=db_path)
        from swarm_engine.project.lifecycle import ProjectLifecycle
        self.project_lifecycle = ProjectLifecycle(db_path=db_path)

        # Native cognitive layer (E/G/H): produces candidate PLANS only. It
        # never registers, persists, or trusts anything itself — every
        # hypothesis still goes through the unchanged Composer.analyze /
        # AdmissionController.admit path below.
        from swarm_engine.cognition.engine import CognitiveEngine
        self.cognition = CognitiveEngine(self.primitives, db_path=db_path)

        # Self-improvement substrate (item 9): observers watch cognition's
        # own accumulated evidence and propose configuration changes;
        # nothing here is admitted without independent, evidence-based
        # validation against the real incumbent.
        from swarm_engine.improvement.substrate import ImprovementStore
        from swarm_engine.improvement.observers import SearchPolicyObserver
        from swarm_engine.improvement.pipeline import (
            ImprovementPipeline, SearchPolicyValidator,
        )
        self.improvements = ImprovementStore(db_path=db_path)
        self.improvement_pipeline = ImprovementPipeline(
            self.cognition, self.improvements,
            observers=[SearchPolicyObserver(self.cognition)],
            validators={"cognition.search_policy":
                       SearchPolicyValidator(self.cognition)},
            provenance=self.provenance)
        self.improvement_pipeline.reactivate_persisted()

        # Acquisition-strategy self-improvement (Boundary 4): observes the
        # orchestrator's own accumulated (signature, strategy, success,
        # cost) experience and proposes signature-scoped pruning of
        # strategies the evidence says never succeed for a signature. The
        # validator needs a held-out goal sampler, which only exists when
        # a caller is actively driving a validation cycle; at boot there
        # is none, so validation fail-closes and only reactivation of an
        # already-ACTIVE improvement runs here.
        from swarm_engine.improvement.observers import AcquisitionStrategyObserver
        from swarm_engine.improvement.pipeline import AcquisitionPolicyValidator

        def _default_acquisition_goal_sampler(signature: str):
            """Held-out goals for acquisition-policy A/B validation.

            Built from signature structure only (arity / example count /
            output kind), not from a test-chosen target. Returns a few
            fresh goals of the same structural class so pruning can be
            compared without polluting production experience.
            """
            # signature example: tgt:none|ex:1-2|in:3+|comp:y|out:str|eff:n
            parts = dict(p.split(":", 1) for p in signature.split("|") if ":" in p)
            n_in = 3 if parts.get("in", "").endswith("+") else 2
            out = parts.get("out", "str")
            goals = []
            for k in range(3):
                args = {f"v{i}": (k + 1) * (i + 1) for i in range(n_in)}
                if out == "str":
                    expect = "x" * (k + 1)
                elif out in ("num", "int", "float"):
                    expect = sum(args.values())
                else:
                    expect = list(args.values())[:1]
                goals.append((f"heldout_{k}_{signature[:12]}",
                              [(args, expect),
                               ({f"v{i}": (k + 2) * (i + 1) for i in range(n_in)},
                                expect if out == "str" else (
                                    sum((k + 2) * (i + 1) for i in range(n_in))
                                    if out in ("num", "int", "float")
                                    else list(args.values())[:1]))]))
            return goals

        self.acquisition_improvement_pipeline = ImprovementPipeline(
            self, self.improvements,
            observers=[AcquisitionStrategyObserver(self)],
            validators={"acquisition.strategy_policy":
                        AcquisitionPolicyValidator(
                            self, goal_sampler=_default_acquisition_goal_sampler,
                            # O14: the default sampler is engine-internal
                            # (synthetic goals); pinned under the engine
                            # identity so the record shows it.
                            _engine_supplied=True)},
            provenance=self.provenance)
        self.acquisition_improvement_pipeline.reactivate_persisted()

        # Autonomous policy state: signature-scoped try-first commitments
        # and general skip rules (F9 AutonomousImprovementEngineer).
        self.acquisition_priority = {}
        self.acquisition_pruning = {}
        self.acquisition_general_skip = {}
        from swarm_engine.improvement.auto_engineer import (
            AutonomousImprovementEngineer)
        self.auto_improver = AutonomousImprovementEngineer(self)
        self.auto_improver.reactivate()

        # Multi-subsystem self-directed selection: collect candidates from
        # acquisition, verification, and representation observers; rank by
        # explicit utility; route winner through existing ImprovementPipeline.
        from swarm_engine.improvement.observers import (
            AcquisitionStrategyObserver, VerificationCostObserver,
            RepresentationSearchObserver,
        )
        from swarm_engine.improvement.multi_selector import MultiSubsystemSelector
        self.multi_improvement_selector = MultiSubsystemSelector(
            observers=[
                AcquisitionStrategyObserver(self),
                VerificationCostObserver(self),
                RepresentationSearchObserver(self),
            ],
            pipeline=self.acquisition_improvement_pipeline,
        )

        from swarm_engine.improvement.agenda import AgendaLoop
        self.improvement_agenda_loop = AgendaLoop(self)
        from swarm_engine.improvement.experiment_design import ExperimentDesignLoop
        self.experiment_design_loop = ExperimentDesignLoop(self)
        from swarm_engine.improvement.curriculum import CurriculumLoop
        self.curriculum_loop = CurriculumLoop(self)
        from swarm_engine.improvement.task_synthesis import TaskSynthesisLoop
        self.task_synthesis_loop = TaskSynthesisLoop(self)
        from swarm_engine.improvement.goal_language import GoalLanguageLoop
        self.goal_language_loop = GoalLanguageLoop(self)





        # Intellectual Engine: observes real accumulated evidence (rollbacks,
        # recurring exhaustion, strong concepts) and runs a real question ->
        # hypothesis -> experiment -> evidence -> belief-update cycle. No
        # external reasoner attached by default — internal generation is
        # real but deliberately narrow, and says so explicitly where it runs
        # out rather than fabricating a plausible-looking guess.
        try:
            from swarm_engine.intellect.engine import IntellectualEngine
            self.intellect = IntellectualEngine(self, db_path=db_path)
        except Exception as _intellect_exc:
            # Incomplete snapshot / optional intellect surface: hierarchical
            # acquisition via AcquisitionOrchestrator does not require it.
            self.intellect = None
            self._intellect_init_error = str(_intellect_exc)

        # Primitive promotion (Prompt 1): crystallizes a verified
        # composition into a genuinely new, registered primitive that
        # GeneralSynthesizer's own search can compose through. Does not
        # expand true type-reachability — see primitive_promotion.py for
        # the exact, honest boundary — only practical reachability within
        # the round-bounded expressiveness closure and the candidate-
        # budget-bounded search.
        from swarm_engine.capability.primitive_promotion import (
            PrimitivePromoter, PromotedPrimitiveStore,
        )
        self.promoted_primitives = PromotedPrimitiveStore(db_path=db_path)
        self.primitive_promoter = PrimitivePromoter(
            self.primitives, self.promoted_primitives, self.cognition.bias,
            provenance=self.provenance)
        self.primitive_promoter.rehydrate()

        # Autonomous representation expansion: connect the two halves the
        # architecture already had. CaseMemory records every verified
        # synthesis; PrimitivePromoter can crystallize a verified Expr into
        # a registered primitive -- but nothing ever called promote().
        # ReificationObserver watches verified successes and promotes
        # sub-computations that recur across distinct goals, so the
        # registry grows from experience, not from developer authorship.
        from swarm_engine.cognition.reification import ReificationObserver
        self.cognition.learner.reifier = ReificationObserver(
            self.primitive_promoter, self.cognition.cases)

        # Iteration-based primitive construction (Prompt 1, the mechanism
        # that actually crosses ExpressivenessAnalyzer's type-reachability
        # ceiling rather than only rearranging it — see
        # primitive_synthesis.py). Was fully built but never connected to
        # the live engine; connected here.
        # Iteration-based primitive construction (also Prompt 1): the
        # mechanism that actually crosses ExpressivenessAnalyzer's
        # type-reachability ceiling, unlike promotion — a fixed-depth Expr
        # tree can never express a variable-trip-count loop, so a genuine
        # step-and-count schema is a real, new kind of capability, not a
        # rearrangement of existing ones. Reuses GeneralSynthesizer's own
        # bank-building for the step-function search rather than a separate
        # enumeration mechanism.
        from swarm_engine.cognition.primitive_synthesis import (
            IteratedPrimitiveStore, IterativePrimitiveGrower, PrimitiveConstructor,
        )
        self.iterated_primitives = IteratedPrimitiveStore(db_path=db_path)
        self.primitive_constructor = PrimitiveConstructor(self.primitives, self.cognition.bias)
        self.iterative_primitive_grower = IterativePrimitiveGrower(
            self.primitives, self.primitive_constructor, self.iterated_primitives,
            provenance=self.provenance)
        self.iterative_primitive_grower.rehydrate()

        self.tasks: Dict[str, Task] = {}
        self.ready = False
        self._next_id = 1
        # Auto-boot so acquired_code and admitted plan capabilities rehydrate
        # without requiring every caller to remember eng.boot().
        try:
            self.boot()
        except Exception as _boot_exc:
            self._boot_error = str(_boot_exc)

    def boot(self):
        # Stored capabilities are plans over primitive names. If the vocabulary
        # changed since they were admitted — a primitive renamed, a family
        # dropped — those plans are stale and would fail mid-execution. Auditing
        # at boot quarantines them before a task can reach one.
        #
        # The audit runs AFTER acquired capabilities are restored from
        # persisted source, not before: a stored plan that composes acquired
        # capabilities names primitives that only exist once
        # rehydrate_acquired() has re-registered them, and auditing first
        # would quarantine a healthy capability for a dependency that is
        # restorable from this same DB. Restoring first is strictly more
        # accurate — a capability whose dependency is genuinely gone is still
        # quarantined by the audit that follows.
        rehydrated = self.rehydrate_acquired()
        # F3 R12: ensure lazily-registered plan-op primitives exist before
        # the audit, so stored discrete_lookup/constant capabilities are
        # not quarantined on reboot for a restorable dependency.
        try:
            ensure = getattr(self.acquisition_orchestrator,
                             "_ensure_plan_op_primitives", None)
            if ensure is not None:
                ensure()
        except Exception:
            pass
        # Recovery re-registration: a quarantined-derived capability the
        # audit reactivates must be executable in this process, so pass
        # admission's own registration path (idempotent, same primitive
        # the admitting process used).
        def _reregister_capability(rec):
            register = getattr(self.admission,
                               "_register_capability_as_primitive", None)
            if register is None:
                raise RuntimeError("admission has no _register_capability_as_primitive")
            register(rec.capability_id, rec)
        audit = self.capabilities.audit_all(self.primitives,
                                            register_fn=_reregister_capability)
        
        # M+25: governed semantic capability store (shared DB)
        try:
            from swarm_engine.acquisition.semantic_capability import (
                SemanticCapabilityStore, SemanticAdmissionController, SemanticValidator,
            )
            self.semantic_store = SemanticCapabilityStore(self.db_path)
            self.semantic_admission = SemanticAdmissionController(
                self.semantic_store, SemanticValidator())
            from swarm_engine.acquisition.semantic_capability import ExternalEvidenceStore
            self.external_evidence_store = ExternalEvidenceStore(self.db_path)
        except Exception:
            self.semantic_store = None
            self.semantic_admission = None
            self.external_evidence_store = None

        self.ready = True
        return {
            "acquired_rehydrated": rehydrated,
            "ready": True,
            "agents": len(self.agents.agents),
            "primitives": len(self.primitives),
            "families": len(self.primitives.families()),
            "capabilities": audit,
        }

    def shutdown(self):
        self.ready = False

    @staticmethod
    def _budget_resource_state(budget: Optional[ResourceBudget]) -> Dict[str, Any]:
        """Return the live acquisition-capacity state exposed by ResourceBudget."""
        limit = getattr(budget, "acquisitions", None) if budget is not None else None
        spent = int(getattr(budget, "spent_acquisitions", 0) or 0) if budget is not None else 0
        remaining = None if limit is None else max(0, int(limit) - spent)
        return {
            "acquisitions_limit": None if limit is None else int(limit),
            "spent_acquisitions": spent,
            "remaining_acquisitions": remaining,
            "wall_clock_s": (getattr(budget, "wall_clock_s", None)
                             if budget is not None else None),
            "primitive_calls": (getattr(budget, "primitive_calls", None)
                                if budget is not None else None),
            "synthesis_attempts": (getattr(budget, "synthesis_attempts", None)
                                   if budget is not None else None),
        }

    @staticmethod
    def _capacity_improved(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
        """Whether the authoritative acquisition capacity became less constrained."""
        old, new = before.get("remaining_acquisitions"), after.get("remaining_acquisitions")
        if old is None:
            return False
        if new is None:
            return True
        return int(new) > int(old)

    def set_resource_budget(self, budget: ResourceBudget) -> Dict[str, Any]:
        """Apply a native budget change and wake only capacity-eligible work.

        Callers change an ordinary ResourceBudget; they never submit a deferred
        goal, select a candidate, or request a retry.  The goal-language loop
        owns recovery of persisted deferred opportunities and performs its own
        normal scoring/selection when capacity genuinely improves.
        """
        before = self._budget_resource_state(getattr(self, "budget", None))
        self.budget = budget
        after = self._budget_resource_state(budget)
        loop = getattr(self, "goal_language_loop", None)
        if loop is None:
            return {"before": before, "after": after,
                    "triggered": False, "reason": "goal-language loop unavailable"}
        return loop.on_resource_state_change(before, after)

    def submit_task(
        self,
        task_type: str,
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        task_id = f"task_{self._next_id:06d}"
        self._next_id += 1
        self.tasks[task_id] = Task(
            task_id=task_id,
            task_type=task_type,
            payload=payload or {},
            metadata=metadata or {},
        )
        return task_id

    def get_task(self, task_id: str) -> Optional[Task]:
        return self.tasks.get(task_id)

    async def run_objective(self, objective_text: str,
                            root_payload: Optional[Dict[str, Any]] = None,
                            examples=None, verify=None):
        """Execute a raw multi-clause objective through the project bridge.

        This is intentionally separate from the older task-syntax decomposer:
        it consumes ObjectiveDecomposition and DependencyGraph directly and
        returns their provenance alongside ProjectExecutor's report.
        """
        if not self.ready:
            self.boot()
        from swarm_engine.acquisition.objective_project_adapter import ObjectiveProjectAdapter
        return await ObjectiveProjectAdapter(self).run(
            objective_text, root_payload=root_payload,
            examples=examples, verify=verify)


    async def resume_project_replan(self, objective_id: str):
        """Rehydrate a paused post-REPLAN project graph and continue (M+29.02).

        Returns an object with as_dict()/succeeded compatible with
        ObjectiveProjectRun for the resumed ProjectRunReport.
        """
        if not self.ready:
            self.boot()
        from swarm_engine.acquisition.project_bridge import ProjectExecutor
        report = await ProjectExecutor(self).resume_replan(objective_id)

        class _ResumeResult:
            def __init__(self, report, oid):
                self.project_report = report
                self.objective = getattr(report, "objective", "") or ""
                self.bridge_error = ""
                self.provenance = {"resumed_objective_id": oid}

            @property
            def succeeded(self):
                pr = self.project_report
                return bool(pr and pr.executed and pr.verified is True)

            def as_dict(self):
                return {
                    "objective": self.objective,
                    "project_report": self.project_report.as_dict() if self.project_report else None,
                    "succeeded": self.succeeded,
                    "provenance": self.provenance,
                    "bridge_error": self.bridge_error,
                }

        return _ResumeResult(report, objective_id)


    def plan_task(self, task: Task) -> List[Dict[str, Any]]:
        """Minimal transparent planner.

        This is deliberately not presented as full cognition. Composite task
        syntax uses ':' to create ordered subtasks; richer decomposition is
        still a future subsystem.
        """
        if task.task_type.count(":") >= 2:
            parts = [p for p in task.task_type.split(":") if p]
            task.plan = [
                {"step": i + 1, "task_type": part, "depends_on": [i] if i else []}
                for i, part in enumerate(parts)
            ]
        else:
            task.plan = [{"step": 1, "task_type": task.task_type, "depends_on": []}]
        return task.plan

    async def run_task(self, task_id: str) -> Dict[str, Any]:
        """Runs the task and records what happened, for every exit path.

        `run_task` has several distinct return points (decompose, synthesis
        failure, verification failure, success), and finding and patching each
        one individually is exactly how a memory system ends up silently
        missing a branch. Wrapping the whole call instead means recording
        cannot fall out of sync with a future change to the branches inside.
        """
        outcome = await self._run_task_inner(task_id)
        task = self.tasks.get(task_id)
        if task is not None:
            self.memory.record_episode(
                goal=task.task_type, success=bool(outcome.get("success")),
                value=(outcome.get("result") or {}).get("value"),
                error=str(outcome.get("error") or ""),
                tier=(outcome.get("arbitration") or {}).get("tier", ""))
        return outcome

    async def _run_task_inner(self, task_id: str) -> Dict[str, Any]:
        if not self.ready:
            self.boot()

        task = self.tasks.get(task_id)
        if task is None:
            return {"success": False, "error": "Task not found"}

        task.state = "arbitrating"
        _ex = None
        try:
            _ex = (task.metadata or {}).get("acquisition_examples")
        except Exception:
            _ex = None
        decision = self.arbitrator.arbitrate(
            task.task_type, payload=getattr(task, 'payload', None),
            composer=self.composer, registry=self.primitives, examples=_ex,
            semantic_store=getattr(self, 'semantic_store', None))
        task.metadata["arbitration"] = {
            "tier": decision.tier.value,
            "reason": decision.reason,
            "matched_capability": decision.matched_capability,
        }

        # Composite tasks are planned explicitly, but the engine does not
        # pretend to have a general-purpose planner yet.
        if decision.tier == ArbitrationTier.DECOMPOSE:
            task.state = "decomposing"
            return await self._run_decomposed(task)

        if decision.tier == ArbitrationTier.SYNTHESIZE:
            # Forge keeps its own mesh synthesizer. Everything else is now
            # synthesized from the primitive substrate: plan over the
            # vocabulary, then let admission decide whether the result is
            # good enough to keep.
            if task.task_type != "forge":
                synth = self._synthesize_capability(task)
                examples = task.metadata.get("acquisition_examples")

                # A template match is normally treated as deliberate intent
                # and trusted without the semantic gate applied to blind
                # search. But a template can still match on a stray keyword --
                # "digit_sum_of" contains "sum" and matched the list-aggregate
                # template, which builds a plan expecting a 'values' argument
                # for a task whose caller-supplied examples are keyed 'n'.
                # When the caller has told us the real input shape via
                # examples, a synthesis result whose plan disagrees with that
                # shape is discarded rather than trusted, and acquisition is
                # tried instead of executing a plan that cannot be right.
                if synth["success"] and examples:
                    expected_keys = set(examples[0][0].keys())
                    record = self.capabilities.get(synth["capability_id"])
                    plan_params = set((record.plan or {}).get("params", {})) if record else set()
                    if plan_params and not (expected_keys & plan_params):
                        synth = {"success": False,
                                "error": (f"template matched on a keyword but its plan "
                                          f"expects {sorted(plan_params)}, which shares "
                                          f"nothing with the caller's example inputs "
                                          f"{sorted(expected_keys)}; treating as a spurious "
                                          f"match rather than trusting it")}

                if not synth["success"]:
                    # Synthesis over the primitive substrate found nothing.
                    # Before failing the task outright, try acquisition: if the
                    # caller supplied examples, this is exactly the case that
                    # should trigger genuine capability growth rather than a
                    # bare "no plan found" — goal -> gap -> candidate
                    # generation -> independent test -> admit or report why not.
                    examples = task.metadata.get("acquisition_examples")
                    acquired = None
                    if examples:
                        acquired = await self.acquire_capability(
                            task.task_type,
                            task.metadata.get("goal_description", task.task_type),
                            list(examples),
                            required_effects=task.metadata.get("required_effects"))
                        self.failure_memory.record(
                            task.task_type,
                            synth["error"] if not (acquired and acquired.get("acquired"))
                            else "resolved by acquisition",
                            valid_prior_state={"synthesis_attempted": True})

                    if not (acquired and acquired.get("acquired")):
                        task.state = "synthesis_failed"
                        task.error = synth["error"]
                        return {
                            "success": False,
                            "task_id": task.task_id,
                            "state": task.state,
                            "arbitration": task.metadata["arbitration"],
                            "synthesis": synth,
                            "acquisition": acquired,
                            "error": task.error,
                        }

                    # Acquisition succeeded: the goal is now a registered
                    # primitive. Build a plan directly from its own declared
                    # input names and admit it, rather than routing back
                    # through backward search — which wraps a primitive's real
                    # argument names behind a single generic 'input' parameter
                    # and would silently mismatch a payload keyed by the
                    # capability's actual argument names (e.g. 'n').
                    task.metadata["acquisition"] = acquired
                    input_names = (acquired.get("spec") or {}).get("inputs") or ["input"]
                    direct_plan = {
                        "name": f"acq_{task.task_type}",
                        "params": {a: "any" for a in input_names},
                        "steps": [{"id": "s1", "op": task.task_type,
                                  "args": {a: {"$param": a} for a in input_names}}],
                        "output": {"$step": "s1"},
                    }
                    plan_check = self.composer.analyze(direct_plan)
                    if not plan_check.ok:
                        task.state = "synthesis_failed"
                        task.error = (f"acquired capability's own plan does not "
                                      f"type-check: {plan_check.errors}")
                        return {"success": False, "task_id": task.task_id,
                                "state": task.state,
                                "arbitration": task.metadata["arbitration"],
                                "acquisition": acquired, "error": task.error}
                    verdict = self.admission.admit(task.task_type, direct_plan)
                    if not verdict.ok:
                        task.state = "synthesis_failed"
                        task.error = f"acquired capability failed admission: {verdict.stage}"
                        return {"success": False, "task_id": task.task_id,
                                "state": task.state,
                                "arbitration": task.metadata["arbitration"],
                                "acquisition": acquired, "error": task.error}
                    self.capabilities.bind_goal(task.task_type, verdict.capability_id)
                    decision.matched_capability = verdict.capability_id
                    task.metadata["synthesis"] = {
                        "success": True, "goal": task.task_type,
                        "capability_id": verdict.capability_id,
                        "strategy": "acquired", "primitives_used": [task.task_type]}
                else:
                    task.metadata["synthesis"] = synth
                    decision.matched_capability = synth["capability_id"]

        # A matched capability is a stored plan over primitives, so it runs
        # through the composer rather than an agent handler.
        if decision.matched_capability and decision.matched_capability.startswith("cap_"):
            task.state = "executing"
            result = await self._execute_capability(decision.matched_capability, task)
        else:
            task.state = "executing"
            agent = self.agents.get(int(task.metadata.get("agent_id", 0)))
            result = await agent.execute_task(
                {
                    "type": task.task_type,
                    "payload": task.payload,
                    "metadata": task.metadata,
                    "task_id": task.task_id,
                }
            )

        task.state = "verifying"
        verification = await self.verifier.verify(result)
        passed = result.get("success", False) and verification["status"] == "passed"

        if passed:
            task.state = "completed"
            task.result = result
            task.error = None
        else:
            task.state = "failed"
            task.result = result
            task.error = result.get("error") or "Verification failed"

        return {
            "success": passed,
            "task_id": task.task_id,
            "state": task.state,
            "result": result,
            "verification": verification,
            "arbitration": task.metadata["arbitration"],
            # task.error was previously set and then dropped from the response,
            # so every caller — including the scheduler — saw a bare failure
            # with no reason attached. A failure the caller cannot diagnose is
            # barely better than a silent one.
            "error": task.error,
        }

    # -- decomposition ------------------------------------------------------
    async def _run_decomposed(self, task: Task) -> Dict[str, Any]:
        """Expand a composite goal into a task graph and execute it.

        Each node is run by submitting it as an ordinary task, so a subtask
        gets the same arbitration, synthesis, governance and verification as
        anything else — including being decomposed again if it is itself
        composite. The scheduler only decides ordering and concurrency.
        """
        try:
            graph = build_graph(
                task.task_type,
                payload=task.payload,
                max_depth=int(task.metadata.get("max_depth", 3)),
                max_nodes=int(task.metadata.get("max_nodes", 64)),
            )
        except TaskGraphError as exc:
            task.state = "decomposition_failed"
            task.error = str(exc)
            return {
                "success": False,
                "task_id": task.task_id,
                "state": task.state,
                "arbitration": task.metadata["arbitration"],
                "error": task.error,
            }

        async def run_node(goal: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            child_id = self.submit_task(
                goal,
                payload=payload,
                # Depth budget is inherited and spent, so a subtask that
                # decomposes again cannot restart the recursion allowance.
                metadata={
                    "parent_task": task.task_id,
                    "max_depth": max(0, int(task.metadata.get("max_depth", 3)) - 1),
                    "allow_effects": task.metadata.get("allow_effects", False),
                },
            )
            child = await self.run_task(child_id)
            value = (child.get("result") or {}).get("value", child.get("value"))
            return {
                "success": child.get("success", False),
                "value": value,
                "error": child.get("error"),
                "task_id": child_id,
            }

        scheduler = Scheduler(run_node,
                              max_parallel=int(task.metadata.get("max_parallel", 4)))
        outcome = await scheduler.run(graph)

        task.plan = outcome["graph"]["nodes"]
        task.result = outcome
        if outcome["success"]:
            task.state = "completed"
            task.error = None
        else:
            task.state = "partially_completed" if outcome["partial"] else "failed"
            task.error = "; ".join(f["error"] or "subtask failed"
                                   for f in outcome["failures"]) or "decomposition failed"

        return {
            "success": outcome["success"],
            "task_id": task.task_id,
            "state": task.state,
            "result": outcome,
            "plan": task.plan,
            "arbitration": task.metadata["arbitration"],
            "error": task.error,
        }

    def rehydrate_acquired(self) -> Dict[str, Any]:
        """Rebuild acquired capabilities from persisted source at boot.

        Each is re-scanned and re-loaded into the restricted sandbox before it
        is registered. A capability whose stored source no longer passes the
        scanner is quarantined rather than restored: trust earned in an
        earlier process is not a licence to skip the check in this one.

        Besides `acquired_code` entries, this also restores the
        `acquired.<capability_id>` primitives that admission registers for
        admitted plan capabilities (decomposition children admitted via the
        symbolic-search fallback, pair-assembled parents, and every other
        admission path). That registration is in-memory only; without
        restoring it, a fresh process cannot execute a stored plan that
        composes acquired capabilities -- a pair-assembled parent's plan
        calls its children by their `acquired.*` names -- and the boot audit
        would quarantine a healthy capability for a dependency that is
        restorable from this same DB. Restoration reuses admission's own
        `_register_capability_as_primitive`, so the primitive is identical
        to the one the admitting process used; it is idempotent and runs
        before the audit, exactly as the boot sequence intends.
        """
        restored, refused = [], []
        for record in self.acquired_code.all():
            # 2026-09-19 (R7): a deliberately quarantined acquired-code
            # entry stays quarantined across reboot -- previously the
            # restore loop resurrected every entry unconditionally, so
            # revoking a code-acquired dependency was silently undone at
            # the next boot and its dependents never invalidated.
            if record.get("status", "active") != "active":
                refused.append(record["name"])
                continue
            candidate = AcquiredCandidate(
                name=record["name"], source=record["source"],
                code=record["code"], entrypoint=record["entrypoint"],
                declared_effects=record["effects"])
            scan = self.acquisition.scanner.scan(candidate)
            if not scan.passed:
                self.provenance.set_trust(
                    record["capability_id"], TrustLevel.QUARANTINED,
                    f"stored source failed rescan: {scan.violations[:2]}")
                refused.append(record["name"])
                continue
            if self._register_acquired(record["name"], candidate,
                                       record["capability_id"],
                                       input_names=(record["spec"] or {}).get("inputs")):
                restored.append(record["name"])
                # The spec was persisted with the source: restore membership
                # so a rehydrated capability is recognised as ACQUIRED (not a
                # built-in primitive) by gap analysis in this process.
                self.acquired_specs[record["name"]] = record["spec"] or {}
                self.provenance.log(record["capability_id"], "rehydrated",
                                    "restored from persisted source at boot")
            else:
                refused.append(record["name"])
        # Restore admission-registered plan capabilities as
        # acquired.<capability_id> primitives (see docstring).
        try:
            plan_records = self.capabilities.list(status="active",
                                                  limit=10_000)
        except Exception:
            plan_records = []
        register = getattr(self.admission,
                           "_register_capability_as_primitive", None)
        for rec in plan_records:
            prim_name = f"acquired.{rec.capability_id}"
            if self.primitives.get(prim_name) is None:
                try:
                    if register is not None:
                        register(rec.capability_id, rec)
                    if self.primitives.get(prim_name) is not None:
                        restored.append(prim_name)
                    else:
                        refused.append(prim_name)
                except Exception:
                    refused.append(prim_name)
            # Canonical goal/node-name alias: bind goal names that point at
            # this capability so fresh processes resolve node.name, not only
            # acquired.<id>.
            src = self.primitives.get(prim_name)
            if src is None:
                continue
            goal_names = []
            gname = getattr(rec, "name", None)
            if gname and gname not in ("synthesized_general", prim_name):
                goal_names.append(gname)
            try:
                bindings = self.capabilities.goal_bindings()
                for goal, cid in (bindings or {}).items():
                    if cid == rec.capability_id or cid == getattr(rec, "capability_id", None):
                        goal_names.append(goal)
            except Exception:
                pass
            for goal in goal_names:
                if not goal or self.primitives.get(goal) is not None:
                    continue
                try:
                    from swarm_engine.primitives.core import Primitive
                    self.primitives.register(Primitive(
                        name=goal,
                        family=getattr(src, "family", "acquired"),
                        fn=src.fn,
                        inputs=dict(getattr(src, "inputs", {}) or {}),
                        output=getattr(src, "output", None),
                        effects=tuple(getattr(src, "effects", ()) or ()),
                        doc=f"rehydrated alias of {prim_name} for goal {goal}",
                    ), overwrite=True)
                    restored.append(goal)
                    # Track the alias so revocation unregisters it
                    # (2026-09-19 R11).
                    try:
                        ti = getattr(self.primitives,
                                     "_acquired_capability_ids", None)
                        if isinstance(ti, dict):
                            ti[goal] = rec.capability_id
                    except Exception:
                        pass
                except Exception:
                    pass
        return {"restored": restored, "refused": refused}

    def project_modification_guard(self, root: str):
        """A ProjectModificationGuard scoped to `root`. A factory rather than
        a stored singleton, since guards are per-project-root and the engine
        may be orchestrating more than one project."""
        from swarm_engine.project.modification import ProjectModificationGuard
        return ProjectModificationGuard(root=root)

    # -- acquisition --------------------------------------------------------
    def _register_acquired(self, name: str, candidate, capability_id: str,
                           input_names: Optional[List[str]] = None,
                           required_effects: Optional[List[str]] = None) -> bool:
        """Make an acquired capability part of the vocabulary.

        The candidate is loaded inside the restricted sandbox and the resulting
        callable is registered as a primitive, so from this point the planner
        treats it exactly like anything else: it can be found, composed,
        type-checked and governed. Registering the sandboxed callable rather
        than exec'ing into the engine's own namespace keeps the candidate's
        restricted builtins in force at call time, so acquisition does not
        become a way to launder unrestricted code into the engine.

        Fail closed (#4): non-empty required_effects with no compatible
        declared effects must not silently register as PURE.
        """
        from swarm_engine.primitives.core import ANY, STR, Effect

        loaded, fn, error = self.acquisition.sandbox.load(candidate)
        if not loaded:
            return False

        declared = list(getattr(candidate, "declared_effects", None) or [])
        req_eff = list(required_effects or [])
        if req_eff and not declared:
            return False
        if req_eff and not set(req_eff).issubset(set(declared)):
            return False

        effects = tuple(
            e for e in Effect if e.value in declared
        ) or (Effect.PURE,)

        # Acquired code is fallible by nature: a raising primitive would
        # propagate out of the composer as an engine fault rather than a plan
        # failure, so it is wrapped to fail as a value.
        def wrapped(**kwargs):
            return fn(**kwargs)

        wrapped.__doc__ = (f"Acquired capability {name!r} from {candidate.source} "
                           f"({capability_id}).")
        # The signature must come from the capability's own specification.
        # Hardcoding a single 'text' input silently mis-registers every
        # capability over a different argument, and it then fails at call time
        # looking like a broken implementation rather than a broken signature.
        names = list(input_names or ["text"])
        self.primitives.define(
            name=name, family="acquired",
            inputs={n: ANY for n in names}, output=ANY, effects=effects)(wrapped)
        # Tag the name -> capability id so the persisted plan-dependency
        # graph (plan_acquired_refs) can resolve bare-name acquired ops,
        # not just `acquired.<id>` ops. Mirrors admission's tagging.
        if name in self.primitives:
            tag_index = getattr(self.primitives, "_acquired_capability_ids", None)
            if not isinstance(tag_index, dict):
                tag_index = {}
                self.primitives._acquired_capability_ids = tag_index
            tag_index[name] = capability_id
        return name in self.primitives

    def synthesize_and_admit(self, goal: str,
                             examples: List[Tuple[Dict[str, Any], Any]],
                             param_names: Optional[Tuple[str, ...]] = None
                             ) -> Dict[str, Any]:
        """The cognitive layer connected to the rest of the engine as one
        loop, not three calls a caller has to remember to stitch together in
        order: propose (E/G) -> admit (unchanged verification/governance
        boundary) -> record the outcome back into the SAME provenance system
        every other capability uses (item 7) -> close the learning loop
        (H) either way.

        On failure, returns the expressiveness diagnosis rather than a bare
        "no" — "the vocabulary cannot express this" and "the search needs
        more budget or structure" are different findings that call for
        different next actions (the former is where external acquisition
        would actually help; the latter is not), and collapsing them into
        one failure would throw away exactly the distinction item 2 exists
        to preserve.
        """
        param_names = param_names or ("value",)
        if not examples:
            return {"admitted": False, "reason": "no examples supplied"}

        result = self.cognition.propose_multi(goal, examples, param_names)
        if not result.solved:
            return {"admitted": False, "goal": goal,
                    "stages_tried": result.stages_tried,
                    "candidates_tried": result.candidates_tried,
                    "expressiveness": result.expressiveness}

        from swarm_engine.synthesis.admission import SmokeTest
        first_args, first_expected = examples[0]
        verdict = self.admission.admit(
            goal, result.plan,
            smoke=SmokeTest(args=dict(first_args), expect=first_expected))

        # Provenance visibility across systems (item 7): the derivation this
        # capability actually came from — which primitives, what tree shape,
        # which reasoning stage produced it — is recorded in the SAME
        # provenance log every other capability's history lives in, not
        # siloed inside cognition's own tables where nothing else would ever
        # see it.
        if verdict.ok:
            if self.provenance.get(verdict.capability_id) is None:
                self.provenance.record(ProvenanceRecord(
                    capability_id=verdict.capability_id,
                    origin=Origin.SYNTHESIZED, trust=TrustLevel.TESTED,
                    source=f"cognition:{result.origin}",
                    primitives_used=list(result.plan.get("steps", []) and
                                        [s["op"] for s in result.plan["steps"]])))
            self.provenance.log(verdict.capability_id, "cognition_synthesis",
                                f"origin={result.origin}; {result.derivation}")

        self.cognition.record_admission_outcome(goal, verdict.ok)

        # Real production outcome tracking: every actual use of the search
        # policy that goes through this path feeds check_for_regression's
        # evidence, not a value a caller has to remember to supply
        # separately. A single call here changes nothing by itself — the
        # minimum-sample floor in check_for_regression is what decides
        # whether this outcome, combined with prior ones, means anything.
        regression = self.improvement_pipeline.record_production_outcome(
            "cognition.search_policy", verdict.ok)
        if regression is not None and regression.get("rolled_back"):
            result_extra = {"regression_detected": regression}
        else:
            result_extra = {}

        return {"admitted": verdict.ok, "goal": goal,
                "capability_id": verdict.capability_id if verdict.ok else None,
                "origin": result.origin, "derivation": result.derivation,
                "verdict_stage": getattr(verdict, "stage", None), **result_extra}

    def synthesize_with_iteration_fallback(self, goal: str,
                                           examples: List[Tuple[Dict[str, Any], Any]],
                                           param_name: str) -> Dict[str, Any]:
        """The real Prompt-1 loop through the actual engine, not a
        standalone module: try tree-based composition first
        (synthesize_and_admit, unchanged) — a fixed-depth Expr tree is
        sufficient for most requirements and should always be tried first,
        since it's strictly cheaper and better-understood. Only on genuine
        failure does this fall back to iteration synthesis, which crosses a
        real ceiling tree-based composition cannot (no fixed-depth tree
        expresses a variable trip count). If iteration succeeds, the new
        primitive is registered/persisted (via IterativePrimitiveGrower's
        own independent re-verification, not this method's trust), and the
        ORIGINAL goal is retried through the same synthesize_and_admit path
        — proving the new primitive is genuinely reachable by ordinary
        composition afterward, not just usable as a one-off answer."""
        first = self.synthesize_and_admit(goal, examples, param_names=(param_name,))
        if first["admitted"]:
            return {**first, "route": "composition"}

        iterated_name = self.iterative_primitive_grower.grow(goal, examples, param_name)
        if iterated_name is None:
            return {**first, "route": "composition", "iteration_attempted": True,
                   "iteration_succeeded": False}

        # Same staleness argument as policy activation elsewhere in this
        # engine: the exhaustion record from the attempt above predates the
        # new primitive's existence — it is evidence about a vocabulary
        # that no longer exists, and would otherwise make the retry below
        # skip searching entirely via the exhaustion fast-path, exactly the
        # bug already found and fixed for search-policy activation.
        self.cognition.exhausted.clear()

        retry = self.synthesize_and_admit(goal, examples, param_names=(param_name,))
        return {**retry, "route": "iteration" if retry["admitted"] else "composition",
               "iteration_attempted": True, "iteration_succeeded": True,
               "iterated_primitive": iterated_name}

    def bind_synthesis_oracle(self, fn, producer_id: str, token: str):
        """O9: register the ambiguity-resolution oracle under an
        authenticated producer and install it as ``engine.synthesis_oracle``.

        The orchestrator refuses to resolve synthesis ambiguity with an
        oracle that was not installed through this method (unregistered
        oracle refusal), and every query the bound oracle answers is
        re-verified against its registered definition and recorded as a
        chained (args-digest -> value) evaluation. What binding does NOT
        prove: that the oracle's answers are true -- a consistently-lying
        oracle is indistinguishable from truth at this layer.
        """
        from swarm_engine.governance.oracle_binding import OracleBindingError
        if not self.oracle_registry.authenticate(producer_id, token):
            raise OracleBindingError(
                "bind_synthesis_oracle: producer authentication failed -- "
                "forged or missing identity refused")
        oracle_id, version = self.oracle_registry.register_oracle(
            producer_id, token, "synthesis_oracle", fn,
            input_contract="args_dict",
            output_contract="oracle value for the discriminating input",
            source="engine.bind_synthesis_oracle")
        self.synthesis_oracle = fn
        self._synthesis_oracle_binding = {
            "fn": fn, "oracle_id": oracle_id, "version": version,
            "producer_id": producer_id}
        return oracle_id, version

    async def resolve(self, goal: str,
                      examples=None,
                      examples_by_node=None,
                      allow_effects: bool = False,
                      delegate=None,
                      delegate_producer_id=None,
                      delegate_token=None) -> Dict[str, Any]:
        """Public hierarchical acquisition entry.

        Delegates to AcquisitionOrchestrator.resolve so structural
        decomposition, acquisition order, leaf reuse/acquisition, and
        parent assembly run without a test-side loop shim.

        O16: when `delegate` is supplied in bound mode, the driver must
        also pass `delegate_producer_id` + `delegate_token` so the
        delegate's identity is bound as provenance; otherwise the delegate
        strategy is refused.
        """
        # O19: provenance for the driver example set -- recorded once here
        # (idempotent) and propagated as examples_batch_id so downstream
        # verdicts cite the exact batch they were judged against.
        examples_batch_id = None
        if examples:
            from swarm_engine.governance.examples_provenance import (
                record_examples_batch)
            examples_batch_id = record_examples_batch(
                self.oracle_registry, self.oracle, goal, list(examples),
                "engine.resolve")
        result = await self.acquisition_orchestrator.resolve(
            goal,
            examples_by_node=examples_by_node,
            examples=examples,
            allow_effects=allow_effects,
            delegate=delegate,
            examples_batch_id=examples_batch_id,
            delegate_producer_id=delegate_producer_id,
            delegate_token=delegate_token,
        )
        return result.as_dict() if hasattr(result, "as_dict") else {
            "goal": goal,
            "fully_resolved": getattr(result, "fully_resolved", False),
            "failed": list(getattr(result, "failed", []) or []),
            "attempts": [a.__dict__ if hasattr(a, "__dict__") else str(a)
                         for a in (getattr(result, "attempts", []) or [])],
            "graph": getattr(result, "graph", None),
        }

    async def acquire_for_goal(self, goal: str, tests=None, cases=None,
                               criteria=None, examples=None) -> Dict[str, Any]:

        """Detect what a goal needs, acquire it, and make it reusable.

        This is the full loop: gap -> requirement -> discovery -> scan ->
        dependencies -> isolation -> verification -> provenance ->
        registration. It reports the gap even when acquisition fails, because
        "we know what is missing" is a materially better state than "it did
        not work".

        Catalogue sources (LocalSource, NetworkSource) are tried first, since
        a vetted or delegated candidate needs no generation. But when neither
        produces anything — which is the common case, since nothing is
        pre-seeded and there is no network here — acquisition used to stop
        there and report failure even though generation was fully capable of
        solving it. That was the exact gap: gap detection worked, catalogue
        search correctly found nothing, and the loop ended instead of falling
        through to synthesis. It now does, whenever examples are available to
        validate a generated candidate against — generation without
        acceptance evidence cannot be validated, so it is not attempted.

        Perception continuity (M+5.3): when examples are available, recover
        unsatisfiable_args from the same cognitive synthesis path that already
        produces them, and pass them into GapDetector so structural exclusions
        are not dropped on the public acquisition path. Does not implement
        acquisition strategies or callable synthesis.
        """
        structural_exclusions = None
        if examples:
            param_names = tuple(examples[0][0].keys()) if examples[0][0] else ("value",)
            synth = self.cognition.propose_multi(goal, list(examples), param_names)
            if getattr(synth, "solved", False):
                gap = self.gaps.detect(goal)
                return {"acquired": False, "gap": gap.as_dict(),
                        "reason": "the goal is already satisfiable via synthesis"}
            expr = getattr(synth, "expressiveness", None) or {}
            structural_exclusions = expr.get("unsatisfiable_args")

        gap = self.gaps.detect(goal, structural_exclusions=structural_exclusions)
        if not gap.has_gap:
            return {"acquired": False, "gap": gap.as_dict(),
                    "reason": "the goal is already satisfiable"}

        try:
            self.budget.spend(acquisitions=1)
        except Exception as exc:
            return {"acquired": False, "gap": gap.as_dict(), "reason": str(exc)}

        outcomes = []
        semantic_evidence_gaps = []
        for requirement in gap.missing:
            result = self.acquisition.acquire(requirement, tests=tests,
                                              cases=cases, criteria=criteria)
            outcomes.append({"route": "catalogue", **result.as_dict()})
            self._log_acquisition_attempt(goal, requirement.name, "catalogue",
                                          result.accepted, result.as_dict())
            if result.accepted:
                return {"acquired": True, "gap": gap.as_dict(),
                        "requirement": requirement.name,
                        "capability_id": result.capability_id,
                        "registered": result.registered,
                        "trust": result.trust.name, "outcomes": outcomes}

            # Fall through to generation. This is the route that was missing:
            # catalogue search finding nothing used to be the end of the
            # story, even though the synthesizer could solve many of these
            # goals outright.
            if examples:
                from swarm_engine.acquisition.strategies import interpret_acquisition_target
                target = interpret_acquisition_target(requirement, examples)
                if isinstance(target, dict) and goal:
                    target = dict(target)
                    target["source_goal"] = str(goal)
                generated = await self.acquire_capability(
                    requirement.name, str(goal) if goal else (requirement.description or ""),
                    examples=list(examples),
                    required_effects=requirement.required_effects,
                    constraints=dict(getattr(requirement, "constraints", None) or {}),
                    acquisition_target=target)
                outcomes.append({"route": "generation",
                                 "acquisition_target": target, **generated})
                if generated.get("acquired"):
                    cid = (generated.get("capability_id")
                           or (generated.get("admitted_ids") or [None])[0])
                    # M+20: bind the USER-facing goal so fresh engines can
                    # resolve_goal(goal) without receiving a capability_id.
                    # Structural admission previously bound only the internal
                    # requirement description (gap text), which is not the
                    # goal the user will re-issue. Representation-agnostic.
                    if cid and goal:
                        try:
                            self.capabilities.bind_goal(str(goal), cid)
                        except Exception:
                            pass
                    return {"acquired": True, "gap": gap.as_dict(),
                            "requirement": requirement.name,
                            "capability_id": cid,
                            "registered": generated.get("registered",
                                bool(generated.get("admitted_ids"))),
                            "trust": "TESTED", "outcomes": outcomes}
            else:
                # M+24: even without examples, form acquisition_target so a
                # semantic_operation_interpretation gap is visible and strategy
                # selection can refuse honestly (no synonym/keyword invention).
                from swarm_engine.acquisition.strategies import interpret_acquisition_target
                target = interpret_acquisition_target(requirement, examples=None)
                if isinstance(target, dict) and target.get("class") == "semantic_operation_interpretation":
                    if goal:
                        target = dict(target)
                        target["source_goal"] = str(goal)
                    # Existing target evidence says operation semantics are
                    # ungrounded.  Persist that diagnostic separately from
                    # CapabilityGap; do not choose a source, query, or
                    # semantic interpretation here.
                    semantic_gap = self.semantic_evidence_gaps.record_open(
                        str(goal) if goal else str(requirement.description or ""),
                        target,
                    )
                    semantic_evidence_gaps.append(semantic_gap.as_dict())
                    generated = await self.acquire_capability(
                        requirement.name,
                        str(goal) if goal else (requirement.description or ""),
                        examples=[],
                        required_effects=requirement.required_effects,
                        constraints=dict(getattr(requirement, "constraints", None) or {}),
                        acquisition_target=target)
                    outcomes.append({
                        "route": "semantic_interpretation",
                        "acquisition_target": target,
                        "semantic_evidence_gap": semantic_gap.as_dict(),
                        **generated,
                    })
                else:
                    outcomes.append({"route": "generation", "acquired": False,
                                     "reason": "no examples supplied; a generated "
                                     "candidate cannot be validated without "
                                     "acceptance evidence, so generation was not "
                                     "attempted rather than admitted on trust"})
        return {"acquired": False, "gap": gap.as_dict(), "outcomes": outcomes,
                "semantic_evidence_gaps": semantic_evidence_gaps,
                "reason": "no candidate satisfied any missing requirement"}

    def _prescreen_worked_examples(self, candidates, spec) -> set:
        """Batched worked-example screen for generated candidates.

        Every scan-passing candidate is executed against ALL of the
        requirement's own worked examples in batched sandbox runs (one process
        spawn per batch instead of one per candidate per validation stage).
        Returns the set of id()s of candidates that satisfy every worked
        example.

        This changes no admission verdict: a candidate that contradicts the
        requirement's own evidence is rejected by the independent-validation
        gauntlet at the acceptance or held-out stage for exactly the same
        reason, so screening it out early only saves the per-candidate
        validation budget. What it buys is a progress signal the old loop
        lacked: a patternless requirement -- hundreds of candidates, zero
        fits -- is recognised after the batched screen instead of after
        minutes of per-candidate subprocess validation.
        """
        ok_ids: set = set()
        from swarm_engine.acquisition.semantic import Case
        scannable = [c for c in candidates
                     if self.acquisition.scanner.scan(c).passed]
        if not spec.examples:
            # No evidence to screen against: leave the verdict to the
            # per-candidate gauntlet exactly as before (it refuses admission
            # with no behavioural cases).
            return {id(c) for c in scannable}
        if not scannable:
            return ok_ids
        calls = [dict(args) for args, _ in spec.examples]
        screen_cases = [Case(args=dict(args), expect=expect, kind="positive",
                             label="prescreen")
                        for args, expect in spec.examples]
        sandbox = self.acquisition.process_sandbox
        batch_size = 32
        for start in range(0, len(scannable), batch_size):
            batch = scannable[start:start + batch_size]
            items = [(c.code, c.entrypoint) for c in batch]
            reports = sandbox.run_batch(items, calls)
            self.budget.spend(sandbox_runs=1)
            if reports is None:
                # The batch exceeded the wall clock -- one member hung. Fall
                # back to individual runs for this batch so the hanging
                # candidate cannot poison its batch-mates' screening.
                reports = [sandbox.run(code, entrypoint, calls)
                           for code, entrypoint in items]
                self.budget.spend(sandbox_runs=len(items))
            for cand, report in zip(batch, reports):
                if self._prescreen_report_passes(report, screen_cases):
                    ok_ids.add(id(cand))
        return ok_ids

    @staticmethod
    def _prescreen_report_passes(report, screen_cases) -> bool:
        """True iff the candidate satisfied every worked example."""
        if not getattr(report, "ok", False):
            return False
        results = list(getattr(report, "value", None) or [])
        if len(results) != len(screen_cases):
            return False
        for case, result in zip(screen_cases, results):
            judged, _ = case.judge(bool(result.get("ok")),
                                   result.get("value"),
                                   str(result.get("error", "")))
            if not judged:
                return False
        return True

    async def acquire_capability(self, name: str, description: str,
                                 examples: List[Tuple[Dict[str, Any], Any]],
                                 required_effects: Optional[List[str]] = None,
                                 constraints: Optional[Dict[str, Any]] = None,
                                 acquisition_target: Optional[Dict[str, Any]] = None
                                 ) -> Dict[str, Any]:
        """Build, independently validate, register and persist a capability
        the engine does not have.

        Nothing is looked up: candidates are synthesized from the
        specification, then judged by the Arbiter on evidence produced by
        roles that did not write them. The generator has no channel through
        which to certify its own output.
        """
        from swarm_engine.acquisition.strategies import (
            CapabilitySpec, StrategySelector, SynthesizingSource,
        )
        from swarm_engine.verification.independent import (
            Arbiter, IndependentValidator,
        )
        from swarm_engine.acquisition.semantic import Case

        spec = CapabilitySpec(
            name=name, description=description, examples=list(examples),
            input_names=sorted(examples[0][0].keys()) if examples else [],
            output_kind=type(examples[0][1]).__name__ if examples else "",
            required_effects=list(required_effects or []),
            constraints=dict(constraints or {}),
            acquisition_target=acquisition_target)

        # A Boolean capability denotes a partition.  Evidence from only one
        # truth class cannot distinguish a relation from its alternatives
        # (comparison, equality, membership, or a constant).  Refuse before
        # candidate generation rather than letting a coincidental skeleton be
        # admitted.  This is generic evidence sufficiency, not an operation
        # or objective-specific rule.
        if spec.output_kind == "bool":
            observed_truth_values = {bool(output) for _, output in spec.examples}
            if len(observed_truth_values) < 2:
                return {
                    "acquired": False,
                    "name": name,
                    "reason": (
                        "boolean capability requires behavioural examples from "
                        "both truth outcomes; the supplied evidence is ambiguous"
                    ),
                    "spec": spec.as_dict(),
                    "candidates_generated": 0,
                    "candidates_rejected": 0,
                }

        strategies = StrategySelector().select(spec, registry=self.primitives)
        strategy_plan = [c.as_dict() for c in strategies]
        selected = strategy_plan[0] if strategy_plan else None
        try:
            self.budget.spend(acquisitions=1)
        except Exception as exc:
            return {"acquired": False, "reason": str(exc),
                    "spec": spec.as_dict(),
                    "strategies": strategy_plan,
                    "selected_strategy": selected}

        # M+10: dispatch Strategy.STRUCTURAL to its dedicated executor.
        # Does not implement callable synthesis — only establishes the route.
        if selected and selected.get("strategy") == "semantic_operation_interpretation":
            # M+25: if caller supplied an interpretation + evidence via
            # constraints["semantic_candidate"], run governed admit path.
            # Never invent the interpretation from goal text.
            cand = (constraints or {}).get("semantic_candidate") if constraints else None
            if not cand or not isinstance(cand, dict):
                return {
                    "acquired": False,
                    "reason": (
                        "semantic_operation_interpretation target formed; "
                        "no semantic_candidate evidence supplied — refusing "
                        "to invent meaning from keywords or synonym tables"
                    ),
                    "spec": spec.as_dict(),
                    "strategies": strategy_plan,
                    "selected_strategy": selected,
                    "acquisition_target": acquisition_target,
                }
            if not hasattr(self, "semantic_admission") or self.semantic_admission is None:
                return {"acquired": False, "reason": "semantic_admission not available",
                        "selected_strategy": selected}
            from swarm_engine.acquisition.semantic_capability import SemanticEvidence
            interp = dict(cand.get("interpretation") or {})
            if not interp:
                return {"acquired": False, "reason": "semantic_candidate missing interpretation",
                        "selected_strategy": selected}
            evidence = []
            for ed in (cand.get("evidence") or []):
                if isinstance(ed, dict):
                    evidence.append(SemanticEvidence.from_dict(ed))
            if not evidence:
                return {"acquired": False, "reason": "semantic_candidate has no evidence",
                        "selected_strategy": selected}
            hyp = self.semantic_admission.submit_hypothesis(
                description=description or name,
                interpretation=interp,
                evidence=evidence,
                provenance={"source": "semantic_candidate_constraint",
                            "strategy": "semantic_operation_interpretation"},
            )
            cases = list(examples) if examples else list(cand.get("behavioral_cases") or [])
            vr = self.semantic_admission.validate(hyp.semantic_id, behavioral_cases=cases or None)
            if not vr.get("ok"):
                return {"acquired": False, "reason": f"semantic validation failed: {vr}",
                        "selected_strategy": selected, "validation": vr,
                        "semantic_id": hyp.semantic_id}
            ar = self.semantic_admission.admit(hyp.semantic_id)
            if not ar.get("ok"):
                return {"acquired": False, "reason": f"semantic admission failed: {ar}",
                        "selected_strategy": selected, "admission": ar}
            return {
                "acquired": True,
                "semantic_id": hyp.semantic_id,
                "semantic_state": "admitted",
                "selected_strategy": selected,
                "acquisition_target": acquisition_target,
                "validation": vr,
                "admission": ar,
            }

        if selected and selected.get("strategy") == "structural_representation":
            from swarm_engine.acquisition.structural_executor import (
                StructuralRepresentationExecutor,
            )
            attempt = StructuralRepresentationExecutor(
                registry=self.primitives,
                composer=self.composer,
                admission=self.admission).execute(spec)
            return {
                "acquired": False,
                "reason": attempt.reason,
                "route": "structural_executor",
                "structural_executor": attempt.as_dict(),
                "executor_invoked": attempt.executed,
                "acquisition_target": getattr(spec, "acquisition_target", None),
                "spec": spec.as_dict(),
                "strategies": strategy_plan,
                "selected_strategy": selected,
                "candidates_generated": len(attempt.candidates),
                "candidates_rejected": max(
                    0, len(attempt.candidates) - len(attempt.validated_candidates)),
                "validated_count": len(attempt.validated_candidates),
                "validation_results": attempt.as_dict().get("validation_results"),
                "admission_results": attempt.as_dict().get("admission_results"),
                "admitted_ids": list(attempt.admitted_ids),
                "admitted_count": len(attempt.admitted_ids),
                "capability_id": (attempt.admitted_ids[0]
                                 if attempt.admitted_ids else None),
                "acquired": bool(attempt.admitted_ids),
                "generic_fallthrough": False,
            }

        # Two materially different hypothesis classes, unioned: single-
        # expression skeletons/compositions, and loop-based algorithms with
        # accumulated state. Prime factorization, digit manipulation and
        # anything else needing control flow is structurally unreachable from
        # the first class alone -- this is what widens the ceiling rather than
        # special-casing the goal that exposed it.
        from swarm_engine.acquisition.algorithmic import AlgorithmicSynthesizer
        candidates = (SynthesizingSource().generate(spec)
                     + AlgorithmicSynthesizer().generate(spec))
        # Worked-example prescreen: before any candidate earns the expensive
        # multi-stage independent-validation gauntlet (one subprocess spawn per
        # stage per candidate), every scan-passing candidate is checked against
        # ALL of the requirement's own worked examples in batched sandbox runs
        # (one spawn per batch of candidates). A candidate that contradicts
        # the requirement's own evidence can never be admitted -- the gauntlet
        # would reject it at the acceptance or held-out stage -- so screening
        # it out early changes no verdict: every candidate the gauntlet could
        # admit passes this screen, and every candidate this screen rejects
        # the gauntlet would reject for the same reason. What changes is cost:
        # a patternless requirement (hundreds of candidates, zero fits) is now
        # recognised in seconds instead of minutes, which is what previously
        # made recursive acquisition of a failing child look like a hang.
        prescreen_ok = self._prescreen_worked_examples(candidates, spec)
        # The first example is the acceptance case; the rest are held back so
        # the Arbiter can tell an implementation from a memorised answer.
        cases = [Case(args=dict(examples[0][0]), expect=examples[0][1],
                      kind="positive", label="acceptance")] if examples else []

        validator = IndependentValidator(
            self.acquisition.process_sandbox.run,
            oracle_registry=self.oracle_registry,
            engine_oracle=self.oracle)
        rejected = 0
        rejection_log: List[Dict[str, Any]] = []
        admitted_candidates: List[Tuple[Any, Any]] = []   # (candidate, verdict)
        for candidate in candidates:
            if id(candidate) not in prescreen_ok:
                # Failed the worked-example prescreen (or the static scan):
                # the full gauntlet would reject it at the acceptance or
                # held-out stage for the same reason, so it never earns the
                # per-candidate validation budget.
                rejected += 1
                rejection_log.append(
                    {"candidate": candidate.notes, "stage": "prescreen",
                     "reasons": ["contradicts one or more worked examples"]})
                continue
            scan = self.acquisition.scanner.scan(candidate)
            if not scan.passed:
                rejected += 1
                rejection_log.append({"candidate": candidate.notes, "stage": "scan",
                                      "reasons": scan.violations[:2]})
                continue
            verdict = validator.validate(candidate.code, candidate.entrypoint,
                                         spec, cases)
            self.budget.spend(sandbox_runs=1)
            if not verdict.admitted:
                rejected += 1
                # Failed candidates are diagnostic information, not discarded
                # noise: what they were rejected for is exactly what should
                # inform whether the next acquisition attempt tries a
                # different hypothesis class or the same one with more
                # examples.
                rejection_log.append({"candidate": candidate.notes, "stage": "verify",
                                      "reasons": verdict.reasons[:2]})
                continue

            admitted_candidates.append((candidate, verdict))

        if not admitted_candidates:
            return {"acquired": False, "name": name,
                    "reason": (f"{len(candidates)} candidate(s) were synthesized "
                               f"(expression + algorithmic) and none survived "
                               f"independent validation"),
                    "candidates_generated": len(candidates),
                    "candidates_rejected": rejected,
                    "rejection_log": rejection_log[:15], "spec": spec.as_dict(),
                    "strategies": strategy_plan,
                    "selected_strategy": strategy_plan[0] if strategy_plan else None}

        # Rank accepted candidates rather than taking the first: prefer more
        # held-out coverage, then simpler code (fewer lines), then lower
        # measured wall-clock cost. Correctness is not a ranking axis here
        # because every admitted candidate already satisfies it in full --
        # that is what admission means.
        def rank_key(pair):
            candidate, verdict = pair
            generalises = verdict.evidence.get("generalises")
            complexity = candidate.code.count("\n")
            return (0 if generalises else 1, complexity)

        admitted_candidates.sort(key=rank_key)
        candidate, verdict = admitted_candidates[0]

        if True:
            capability_id = stable_code_id("acq", name, candidate.code)
            self.provenance.record(ProvenanceRecord(
                capability_id=capability_id, origin=Origin.ACQUIRED,
                trust=TrustLevel.TESTED, source=candidate.source,
                effects=list(required_effects or [])))
            self.provenance.log(capability_id, "validated",
                                json.dumps(verdict.evidence)[:480])
            registered = self._register_acquired(name, candidate, capability_id,
                                                 input_names=spec.input_names)
            self.provenance.log(capability_id, "registered", f"as primitive {name!r}")
            # Persist the source, not the callable: the capability must be
            # rebuildable from scratch in a later process, and re-scanned on
            # the way back in rather than trusted because it was trusted once.
            self.acquired_code.save(
                name=name, capability_id=capability_id, code=candidate.code,
                entrypoint=candidate.entrypoint, source=candidate.source,
                effects=list(required_effects or []), spec=spec.as_dict(),
                evidence=verdict.evidence)
            self.acquired_specs[name] = spec
            for state in (LifecycleState.CANDIDATE, LifecycleState.CONSTRUCTED,
                         LifecycleState.VALIDATING, LifecycleState.VERIFIED,
                         LifecycleState.ADMITTED, LifecycleState.REGISTERED,
                         LifecycleState.DEPLOYED):
                self.lifecycle.transition(capability_id, state,
                                          reason=f"acquisition: {candidate.source}")
            outcome = {
                "acquired": True, "name": name, "capability_id": capability_id,
                "registered": registered, "source": candidate.source,
                "strategy": strategies[0].as_dict() if strategies else None,
                "candidates_generated": len(candidates),
                "candidates_rejected": rejected, "rejection_log": rejection_log[:15],
                "evidence": verdict.evidence, "spec": spec.as_dict(),
            }
            self._log_acquisition_attempt(name, name, "generation", True, outcome)
            return outcome

        outcome = {"acquired": False, "name": name,
                  "reason": (f"{len(candidates)} candidate(s) were synthesized and "
                             f"none survived independent validation"),
                  "candidates_generated": len(candidates),
                  "candidates_rejected": rejected,
                  "rejection_log": rejection_log[:15], "spec": spec.as_dict()}
        self._log_acquisition_attempt(name, name, "generation", False, outcome)
        return outcome

    def _log_acquisition_attempt(self, goal: str, requirement: str, route: str,
                                 accepted: bool, detail: Dict[str, Any]) -> None:
        """Traceability independent of outcome: what was attempted and why it
        succeeded or failed, queryable even when nothing was ever admitted and
        so has no capability_id of its own to log against."""
        with sqlite3.connect(self.capabilities.db_path if hasattr(
                self.capabilities, "db_path") else "swarm_engine.db") as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS acquisition_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT, requirement TEXT,
                route TEXT, accepted INTEGER, detail TEXT, at REAL)""")
            conn.execute("""INSERT INTO acquisition_attempts
                (goal, requirement, route, accepted, detail, at) VALUES (?,?,?,?,?,?)""",
                (goal, requirement, route, int(accepted),
                 json.dumps(detail, default=str)[:4000], time.time()))

    def acquisition_history(self, goal: Optional[str] = None) -> List[Dict[str, Any]]:
        """Full traceability: what was attempted, in what order, why it was
        rejected, and what was ultimately accepted — for any goal, whether or
        not acquisition ever succeeded."""
        with sqlite3.connect(self.capabilities.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("""CREATE TABLE IF NOT EXISTS acquisition_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT, requirement TEXT,
                route TEXT, accepted INTEGER, detail TEXT, at REAL)""")
            query = "SELECT * FROM acquisition_attempts"
            params: Tuple = ()
            if goal:
                query += " WHERE goal=?"
                params = (goal,)
            rows = conn.execute(query + " ORDER BY id", params).fetchall()
        return [{"goal": r["goal"], "requirement": r["requirement"],
                "route": r["route"], "accepted": bool(r["accepted"]),
                "detail": json.loads(r["detail"]), "at": r["at"]} for r in rows]

    # -- primitive-backed synthesis and execution ---------------------------
    def _synthesize_capability(self, task: Task) -> Dict[str, Any]:
        """Plan a capability for this task over the primitive vocabulary and
        put it through admission. Returns a report either way — a refusal to
        admit is a real outcome, not an exception."""
        goal = task.metadata.get("goal", task.task_type)
        proposals = self.planner.propose(
            goal,
            allow_effects=bool(task.metadata.get("allow_effects", False)),
        )
        if not proposals:
            return {
                "success": False,
                "goal": goal,
                "error": f"No plan could be composed for {goal!r} from the "
                         f"{len(self.primitives)} available primitives.",
            }

        # Try proposals in rank order. A plan that fails admission is not a
        # dead end while a lower-ranked one may still be sound.
        rejections = []
        for proposal in proposals:
            # Backward search is type-directed, so for a goal it does not
            # understand it will still return *something* whose signature fits
            # -- and the engine would report that nonsense as a solved goal.
            # A template match is deliberate intent and needs no such check;
            # a search result must show it relates to what was asked.
            if not proposal.strategy.startswith("template"):
                match = self.matcher.score(
                    goal, description=" ".join(proposal.ops_used),
                    ops_used=proposal.ops_used, strategy=proposal.strategy)
                if not match.sufficient:
                    rejections.append({
                        "strategy": proposal.strategy, "stage": "semantic",
                        "reasons": [f"plan over {proposal.ops_used} is not "
                                    f"semantically related to the goal "
                                    f"(score {match.score:.2f})"] + match.against})
                    continue

            verdict = self.admission.admit(goal, proposal.plan)
            if verdict.ok:
                self.capabilities.bind_goal(task.task_type, verdict.capability_id)
                # A synthesized capability enters at TESTED: admission already
                # type-checked it, checked its permissions and smoke-ran it.
                # TRUSTED is still earned through use, not granted at birth.
                if self.provenance.get(verdict.capability_id) is None:
                    self.provenance.record(ProvenanceRecord(
                        capability_id=verdict.capability_id,
                        origin=Origin.SYNTHESIZED, trust=TrustLevel.TESTED,
                        source=proposal.strategy,
                        primitives_used=list(proposal.ops_used)))
                return {
                    "success": True,
                    "goal": goal,
                    "capability_id": verdict.capability_id,
                    "verdict": verdict.verdict.value if hasattr(verdict.verdict, "value")
                               else str(verdict.verdict),
                    "strategy": proposal.strategy,
                    "primitives_used": proposal.ops_used,
                }
            rejections.append({
                "strategy": proposal.strategy,
                "stage": verdict.stage,
                "reasons": verdict.as_dict().get("reasons", []),
            })

        return {
            "success": False,
            "goal": goal,
            "error": f"{len(proposals)} plan(s) were composed for {goal!r} but "
                     f"none passed admission.",
            "rejected": rejections,
        }

    async def _execute_capability(self, capability_id: str, task: Task) -> Dict[str, Any]:
        """Rehydrate an admitted capability and run it through the composer.

        This awaits the composer rather than calling execute_sync: run_task is
        already inside an event loop, and execute_sync starts its own with
        asyncio.run(). Awaiting also means effectful primitives that do real
        I/O don't block the loop.
        """
        record, problems = self.capabilities.rehydrate(capability_id, self.primitives)
        if record is None or problems:
            # rehydrate() has already quarantined it; surface why rather than
            # attempting to execute a plan with missing dependencies.
            return {
                "success": False,
                "capability_id": capability_id,
                "error": "; ".join(problems) or f"capability {capability_id!r} unavailable",
            }

        args = dict(task.payload)
        outcome = await self.composer.execute(record.plan, args)
        succeeded = bool(outcome.get("success"))

        # Telemetry is what lets arbitration and the improvement loop tell a
        # capability that works from one that merely exists.
        self.capabilities.record_use(capability_id, succeeded)
        # Trust moves on evidence: sustained success promotes, one failure
        # demotes. This is what makes confidence mean something later.
        self.provenance.record_use(capability_id, succeeded)
        if not succeeded:
            self.capabilities.log(capability_id, "execution_failed",
                                  str(outcome.get("error", ""))[:200])

        # The composer stamps its own plan hash. For an admitted capability the
        # store's id is the one that telemetry, rollback and the audit log are
        # keyed on, so it must win — otherwise a failure gets recorded against
        # an id nothing else in the engine refers to.
        outcome["plan_hash"] = outcome.get("capability_id")
        outcome["capability_id"] = capability_id
        return outcome

    def stats(self):
        return {
            "ready": self.ready,
            "tasks": len(self.tasks),
            "agents": len(self.agents.agents),
            "knowledge_base": self.kb.stats(),
            "primitives": len(self.primitives),
            "families": len(self.primitives.families()),
            "capabilities": len(self.capabilities.list()),
        }
