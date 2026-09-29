"""runtime/curiosity/frm -- Financial Resource Manager evaluation layer.

Phase 1, mission CUR-P1C. Implements the FRM amendment's open machinery:

  4. FRM policy evaluation layer
  5. Epoch-bounded lending semantics in the integrated FRM/arbitrator path

James (2026-09-29, verbatim amendment) defines the value function, cost
ceilings, resource ceilings, Primary's guaranteed operating minimum, the
conditions under which expensive resources are permitted, and the standing
allocation policy. The FRM evaluates that policy each contention round
against measured demand, availability, cost inputs, and (provisional)
expected yield; the EXISTING ResourceArbitrator -- a second INSTANCE of the
same class the Primary path uses, never a second class (no-duplication
rule) -- maps the policy+demand to grants. Run Controllers spend grants;
grants are non-preemptive ceilings.

C-8.1 preserved: the FRM holds no objectives of its own. Every value
judgment in this package (minimums, ceilings, WARNING_1 restriction,
expensive-model authorization) is a James-set standing-policy INPUT;
the FRM only evaluates it. Expected-yield heuristics are explicitly
PROVISIONAL (code, docstrings, round records) until empirical yield
feedback (CUR-P1D machinery) is wired in.
"""

from swarm_engine.curiosity.frm.policy import (
    BANNED_6M,
    ENFORCEMENT_STATES,
    HARD_SHUTDOWN_RESOURCE,
    RUNNING,
    SUSPENDED_SAFETY,
    WARNING_1,
    ZERO_ALLOCATION_STATES,
    CostInput,
    CostKind,
    DomainDemand,
    ExpensiveModelPolicy,
    ExpectedYield,
    FrmPolicy,
)
from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord
from swarm_engine.curiosity.frm.ledger import EpochLedger
from swarm_engine.curiosity.frm.evaluation import (
    FinancialResourceManager,
    FrmRound,
    EpochClose,
)

__all__ = [
    "BANNED_6M",
    "ENFORCEMENT_STATES",
    "HARD_SHUTDOWN_RESOURCE",
    "RUNNING",
    "SUSPENDED_SAFETY",
    "WARNING_1",
    "ZERO_ALLOCATION_STATES",
    "CostInput",
    "CostKind",
    "DomainDemand",
    "ExpensiveModelPolicy",
    "ExpectedYield",
    "FrmPolicy",
    "FrmGrant",
    "LendingRecord",
    "EpochLedger",
    "FinancialResourceManager",
    "FrmRound",
    "EpochClose",
]
