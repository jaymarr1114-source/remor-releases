"""runtime/curiosity/frm/policy.py

James's standing allocation-policy inputs to the FRM evaluation layer.

C-8.1 boundary: everything in this module is a POLICY INPUT set by James
(the value function, ceilings, Primary's guaranteed minimum, WARNING_1
restriction level, expensive-resource authorization). The FRM consumes
these; it does not choose them. A "policy" that the FRM invented for
itself would be a third-executive violation, not an input.

Frozen interface values carried here:
  - Enforcement states: RUNNING | HARD_SHUTDOWN_RESOURCE | WARNING_1 |
    SUSPENDED_SAFETY | BANNED_6M (P1B owns the real state machine; the FRM
    takes the state as a validated input label).
  - FRM grant shape is emitted by grant.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional


# ---------------------------------------------------------------------------
# Enforcement states (frozen interface; P1B owns the real machine)
# ---------------------------------------------------------------------------

RUNNING = "RUNNING"
HARD_SHUTDOWN_RESOURCE = "HARD_SHUTDOWN_RESOURCE"
WARNING_1 = "WARNING_1"
SUSPENDED_SAFETY = "SUSPENDED_SAFETY"
BANNED_6M = "BANNED_6M"

ENFORCEMENT_STATES = frozenset(
    {RUNNING, HARD_SHUTDOWN_RESOURCE, WARNING_1, SUSPENDED_SAFETY, BANNED_6M}
)

# Any active enforcement state means zero allocation to Curiosity for the
# round (frozen interface: "zero allocation while any enforcement state is
# active"). WARNING_1 restricts instead of zeroing, per the decided D-3
# per-level rules (first qualifying violation carries restrictions).
ZERO_ALLOCATION_STATES = frozenset(
    {HARD_SHUTDOWN_RESOURCE, SUSPENDED_SAFETY, BANNED_6M}
)


# ---------------------------------------------------------------------------
# Cost inputs (FRM amendment section 8)
# ---------------------------------------------------------------------------

class CostKind:
    """Provenance of a monetary-cost input. The architecture must
    distinguish measured cost from declared/estimated/unpriced."""
    MEASURED = "MEASURED"      # observed billing fact
    DECLARED = "DECLARED"      # policy-declared price constant
    ESTIMATED = "ESTIMATED"    # provisional cost estimate
    UNPRICED = "UNPRICED"      # no cost information available

    ALL = frozenset({MEASURED, DECLARED, ESTIMATED, UNPRICED})


@dataclass(frozen=True)
class CostInput:
    """One monetary-cost input to a contention round.

    value is None exactly when kind is UNPRICED. provenance names where
    the number came from (provider invoice, policy constant, estimator
    name) so a later audit can tell observed billing facts apart from
    policy inputs.
    """
    kind: str
    value: Optional[float]
    provenance: str

    def __post_init__(self) -> None:
        if self.kind not in CostKind.ALL:
            raise ValueError(f"unknown cost kind {self.kind!r}")
        if self.kind == CostKind.UNPRICED and self.value is not None:
            raise ValueError("UNPRICED cost inputs carry no value")
        if self.kind != CostKind.UNPRICED and self.value is None:
            raise ValueError(f"{self.kind} cost inputs require a value")
        if self.value is not None and self.value < 0:
            raise ValueError("cost value must be >= 0")


# ---------------------------------------------------------------------------
# Expected yield (PROVISIONAL until CUR-P1D yield attribution is wired)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExpectedYield:
    """Expected accepted-capability yield for a domain's demand.

    PROVISIONAL: until the yield-attribution machinery (FRM amendment
    section 18 item 1; sibling mission CUR-P1D) produces empirical
    attribution, no measured yield exists. This heuristic is recorded in
    each round for audit but is NON-BINDING: it never overrides Primary's
    guaranteed minimum, enforcement gating, or the arbitrator's mechanical
    mapping. Marked provisional here, in the round record, and in the
    final report.
    """
    value: float
    basis: str
    provisional: bool = True

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("expected yield must be >= 0")
        if not self.provisional:
            # The empirical feedback path does not exist yet; claiming a
            # non-provisional yield would be fabricated measurement.
            raise ValueError(
                "ExpectedYield may not be marked non-provisional: empirical "
                "yield attribution is not wired (CUR-P1D boundary)")


# ---------------------------------------------------------------------------
# Domain demand (test doubles in Phase 1; Run Controllers in Phase 2+)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DomainDemand:
    """What one executive domain asks for in the coming epoch."""
    domain: str                      # "primary" | "curiosity"
    budget_s: float = 0.0
    max_concurrent: int = 0
    expected_yield: ExpectedYield = field(
        default_factory=lambda: ExpectedYield(
            value=0.0, basis="no yield information"))
    requests_expensive_model: bool = False
    expensive_model_name: Optional[str] = None

    def __post_init__(self) -> None:
        if self.domain not in ("primary", "curiosity"):
            raise ValueError(f"domain must be primary|curiosity, got {self.domain!r}")
        if self.budget_s < 0 or self.max_concurrent < 0:
            raise ValueError("demand must be >= 0")
        if self.requests_expensive_model and not self.expensive_model_name:
            raise ValueError("expensive model request must name the model")


# ---------------------------------------------------------------------------
# Expensive-model authorization (FRM amendment section 9)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExpensiveModelPolicy:
    """James's standing conditions for expensive reasoning resources.

    The FRM evaluates these conditions; it cannot authorize an expensive
    model outside them (amendment section 9).
    """
    enabled: bool = False
    allowed_models: FrozenSet[str] = frozenset()
    conditions: Dict[str, str] = field(default_factory=dict)

    def authorizes(self, model_name: str) -> bool:
        return self.enabled and model_name in self.allowed_models


# ---------------------------------------------------------------------------
# The standing policy (James's inputs)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FrmPolicy:
    """James's standing allocation policy.

    - total_*: the measurable resource envelope for the epoch.
    - primary_minimum_*: Primary's guaranteed operating minimum (FRM
      amendment section 11). Curiosity can never consume it; the guarantee
      is implemented by the allocation mechanism (Primary's arbitrator
      minimum), not by Curiosity cooperation.
    - curiosity_ceiling_*: James's resource ceilings for the curiosity
      domain (amendment section 2: "cost ceilings; resource ceilings").
    - cost_ceilings: per-cost-kind or per-model monetary caps (policy
      constants; provenance recorded on each CostInput).
    - warning_1_cap_fraction: the WARNING_1 restriction level. The
      decided D-3 rules say WARNING_1 carries restrictions; the exact
      restriction is a standing policy input, NOT an FRM choice.
      Curiosity demand is scaled to this fraction of its stated demand
      while WARNING_1 is active.
    - primary_weight / curiosity_weight: standing allocation weights fed
      to the arbitrator's mechanical distribution.
    - expensive_models: James's expensive-resource authorization.
    - epoch_s: contention cadence (default 300s per amendment section 15).
    """
    total_budget_s: float
    total_max_concurrent: int
    primary_minimum_budget_s: float
    primary_minimum_concurrent: int
    primary_weight: float = 1.0
    curiosity_weight: float = 1.0
    curiosity_ceiling_budget_s: Optional[float] = None
    curiosity_ceiling_concurrent: Optional[int] = None
    cost_ceilings: Dict[str, float] = field(default_factory=dict)
    warning_1_cap_fraction: float = 0.25
    expensive_models: ExpensiveModelPolicy = field(
        default_factory=ExpensiveModelPolicy)
    epoch_s: float = 300.0

    def __post_init__(self) -> None:
        if not (self.total_budget_s > 0):
            raise ValueError("total_budget_s must be > 0")
        if not (self.total_max_concurrent > 0):
            raise ValueError("total_max_concurrent must be > 0")
        if self.primary_minimum_budget_s < 0 or self.primary_minimum_concurrent < 0:
            raise ValueError("primary minimums must be >= 0")
        if (self.primary_minimum_budget_s > self.total_budget_s
                or self.primary_minimum_concurrent > self.total_max_concurrent):
            # Fail-closed configuration error: a guarantee larger than the
            # envelope cannot be honored. Loud, never silently scaled.
            raise ValueError(
                "primary minimum exceeds total capacity: the guaranteed "
                f"minimum ({self.primary_minimum_budget_s}s/"
                f"{self.primary_minimum_concurrent} slots) cannot fit in "
                f"the envelope ({self.total_budget_s}s/"
                f"{self.total_max_concurrent} slots)")
        if not (self.primary_weight > 0 and self.curiosity_weight > 0):
            raise ValueError("weights must be > 0")
        if not (0.0 < self.warning_1_cap_fraction <= 1.0):
            raise ValueError("warning_1_cap_fraction must be in (0, 1]")
        if not (self.epoch_s > 0):
            raise ValueError("epoch_s must be > 0")
        for name, v in (("curiosity_ceiling_budget_s", self.curiosity_ceiling_budget_s),
                        ("curiosity_ceiling_concurrent", self.curiosity_ceiling_concurrent)):
            if v is not None and v < 0:
                raise ValueError(f"{name} must be >= 0")
