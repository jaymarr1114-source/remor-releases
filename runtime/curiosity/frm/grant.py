"""runtime/curiosity/frm/grant.py

The FRM grant record: the frozen grant shape shared across the five
Phase-1 missions.

  {grant_id, epoch_id, epoch_s (default 300), dimensions: {budget_s,
   max_concurrent}, primary_minimum: {budget_s, max_concurrent},
   lent: bool}

Grants are immutable (frozen dataclass): a grant issued for an epoch
cannot be shrunk mid-execution. Non-preemption is structural, not a
convention the FRM promises to remember.

Lending (FRM amendment section 14): capacity temporarily lent from
Primary's unused guaranteed minimum to Curiosity is epoch-bounded.
lent=True marks a grant carrying lent capacity; the loan is recalled at
the epoch boundary (see evaluation.advance_epoch). Running work admitted
against the grant is never preempted: recall affects the NEXT
allocation decision and admission, never running work.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet


# The frozen grant shape: as_dict() must emit exactly these keys.
GRANT_KEYS: FrozenSet[str] = frozenset({
    "grant_id", "epoch_id", "epoch_s", "dimensions",
    "primary_minimum", "lent",
})


@dataclass(frozen=True)
class LendingRecord:
    """Epoch-bounded lending detail attached to a curiosity grant."""
    lent_budget_s: float
    lent_concurrent: int
    source: str = "primary_unused_minimum"
    recalled: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "lent_budget_s": self.lent_budget_s,
            "lent_concurrent": self.lent_concurrent,
            "source": self.source,
            "recalled": self.recalled,
        }


@dataclass(frozen=True)
class FrmGrant:
    """One domain's grant for one epoch. Immutable by construction."""
    grant_id: str
    epoch_id: int
    epoch_s: float
    domain: str
    budget_s: float
    max_concurrent: int
    primary_minimum_budget_s: float
    primary_minimum_concurrent: int
    lent: bool
    lending: LendingRecord = field(
        default_factory=lambda: LendingRecord(0.0, 0))
    enforcement_state_at_issue: str = "RUNNING"
    issued_at: float = 0.0
    note: str = ""

    @classmethod
    def issue(cls, *, domain: str, epoch_id: int, epoch_s: float,
              budget_s: float, max_concurrent: int,
              primary_minimum_budget_s: float,
              primary_minimum_concurrent: int,
              lent: bool, lending: LendingRecord,
              enforcement_state_at_issue: str,
              issued_at: float, note: str = "") -> "FrmGrant":
        return cls(
            grant_id=str(uuid.uuid4()),
            epoch_id=epoch_id,
            epoch_s=epoch_s,
            domain=domain,
            budget_s=budget_s,
            max_concurrent=max_concurrent,
            primary_minimum_budget_s=primary_minimum_budget_s,
            primary_minimum_concurrent=primary_minimum_concurrent,
            lent=lent,
            lending=lending,
            enforcement_state_at_issue=enforcement_state_at_issue,
            issued_at=issued_at,
            note=note,
        )

    def as_dict(self) -> Dict[str, Any]:
        """The frozen grant shape: exactly the six shared keys.

        Internal bookkeeping (domain, lending detail, enforcement state
        at issue, timestamps) rides in the round record, not the grant.
        """
        d = {
            "grant_id": self.grant_id,
            "epoch_id": self.epoch_id,
            "epoch_s": self.epoch_s,
            "dimensions": {
                "budget_s": self.budget_s,
                "max_concurrent": self.max_concurrent,
            },
            "primary_minimum": {
                "budget_s": self.primary_minimum_budget_s,
                "max_concurrent": self.primary_minimum_concurrent,
            },
            "lent": self.lent,
        }
        assert frozenset(d.keys()) == GRANT_KEYS, \
            f"grant shape drift: {sorted(d.keys())}"
        return d
