"""The creativity executive package (runtime/creativity/).

Peer-level executive domain — NOT under runtime/curiosity/ (which is
the FRM/second-executive domain). The creativity executive is the
third executive on paper; this package holds the paper-SPECIFIED
slices James's decisions settled, built easiest-first in causal order:

- D-5 (paper §9 T2, §2): the six ordered creative stages
  (stages.py) — CREATIVITY-SLICE-1.
- D-4 (paper §9 T1): the intent register with intent-state ownership
  (intent.py) — CREATIVITY-SLICE-1.
- §3 mechanism: the verified-substrate ledger, read-side only
  (ledger.py) — CREATIVITY-LEDGER-1. Compositions draw exclusively
  from the verified side; unverified demand becomes a named gap,
  never substrate.
- §6 machinery: the critique pipeline — novelty, value, acceptance
  panels (critique.py) — CREATIVITY-CRITIQUE-1.
- The admission contract: the gallery admission record with full
  provenance (release.py) — CREATIVITY-RELEASE-1.
- The resource model: compute+monetary spend through the FRM grant
  path, ceilings, authorizations (budget.py) — CREATIVITY-BUDGET-1.
- The executive: T0–T9 — commission, six loop controllers,
  composition search, critique wiring, release, boundary hook,
  kill ladder (executive.py) — CREATIVITY-EXEC-1.
- The conditional run controller: activation predicates, real budget
  grant at open, measured-actual spend, ceiling stops, the shared
  kill event, RunRecords (run_controller.py) — CREATIVITY-RUNCTRL-1.

TRACK COMPLETE (CREATIVITY-INTEGRATE-1): all eight modules are
crossed+landed and proven as one integrated system — the end-to-end
battery (proofs/creativity_integrate1/) runs a commissioned creative
task through the real call path with every track battery green at one
HEAD. Nothing further is built here; this package is the track's
final state.

The dual-executive build hold was lifted 2026-10-01 (only dispatch is
not-to-build); peer arbitration (D-7) remains unbuilt and is NOT
exported here — there is no arbitration module in this package.
"""

from .budget import (
    COMPUTE,
    MONETARY,
    AuthorizationRecord,
    BudgetEnvelope,
    BudgetHalted,
    BudgetRefused,
    CeilingStop,
    CostBound,
    CreativityBudget,
    SpendLedger,
    propose_envelope,
)
from .critique import (
    CandidateComposition,
    CritiqueRefused,
    CritiqueReport,
    CriterionResult,
    NoveltyVerdict,
    ValueScore,
    canonical_signature,
    check_novelty,
    critique_candidate,
    critique_value,
    submit_to_panels,
)
from .executive import (
    EXECUTIVE_IDENTITY,
    CreativeLoopController,
    CreativityExecutiveController,
    ExecutiveOutcome,
    ExecutiveRefused,
    LoopStep,
    MicrocontrollerRegistry,
    SearchBounds,
    classify_ambiguity,
)
from .intent import (
    CreativeIntent,
    IntentRefused,
    admissible_stages,
    register_intent,
    select_state,
)
from .ledger import (
    DEVICE_CONFIRMED,
    DEVICE_NOT_APPLICABLE,
    DEVICE_UNCONFIRMED,
    CompositionVerdict,
    Ledger,
    LedgerEntry,
    LedgerRefused,
)
from .release import (
    CRITERIA_VERSION,
    AdmissionRecord,
    AdmissionRefused,
    AdmissionRegistry,
    CriterionOutcome,
    ReleaseResult,
    RemovalRecord,
    StageOutcome,
    admit,
    record_removal,
    run_release_flow,
)
from .run_controller import (
    CreativityRunController,
    PredicateOutcome,
    RunControllerRefused,
    RunRecord,
)
from .stages import (
    STAGE_ORDER,
    CreativeStage,
    StageDescriptor,
    StageRefused,
    admissible_next,
    describe,
    is_terminal,
    refinement_loop_admissible,
)

__all__ = [
    "COMPUTE",
    "CRITERIA_VERSION",
    "EXECUTIVE_IDENTITY",
    "MONETARY",
    "STAGE_ORDER",
    "AdmissionRecord",
    "AdmissionRefused",
    "AdmissionRegistry",
    "AuthorizationRecord",
    "BudgetEnvelope",
    "BudgetHalted",
    "BudgetRefused",
    "CandidateComposition",
    "CeilingStop",
    "CostBound",
    "CreativeIntent",
    "CreativeLoopController",
    "CreativeStage",
    "CreativityBudget",
    "CreativityExecutiveController",
    "CreativityRunController",
    "CriterionOutcome",
    "CriterionResult",
    "CritiqueRefused",
    "CritiqueReport",
    "DEVICE_CONFIRMED",
    "DEVICE_NOT_APPLICABLE",
    "DEVICE_UNCONFIRMED",
    "CompositionVerdict",
    "ExecutiveOutcome",
    "ExecutiveRefused",
    "IntentRefused",
    "Ledger",
    "LedgerEntry",
    "LedgerRefused",
    "LoopStep",
    "MicrocontrollerRegistry",
    "NoveltyVerdict",
    "PredicateOutcome",
    "ReleaseResult",
    "RemovalRecord",
    "RunControllerRefused",
    "RunRecord",
    "SearchBounds",
    "SpendLedger",
    "StageDescriptor",
    "StageRefused",
    "StageOutcome",
    "ValueScore",
    "admissible_next",
    "admissible_stages",
    "admit",
    "canonical_signature",
    "check_novelty",
    "classify_ambiguity",
    "critique_candidate",
    "critique_value",
    "describe",
    "is_terminal",
    "propose_envelope",
    "record_removal",
    "refinement_loop_admissible",
    "register_intent",
    "run_release_flow",
    "select_state",
    "submit_to_panels",
]
