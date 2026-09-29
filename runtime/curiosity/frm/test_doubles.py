"""runtime/curiosity/frm/test_doubles.py

Phase-1 stand-ins for the demand and spending sides of the FRM path.

Phase 2 owns the real producers/consumers (Curiosity Executive, Run
Controllers, loops); this mission's mandate is explicit: "demand comes
from test doubles." These doubles are honest: they really state demand
and really spend against grants -- they are not scripted green lights.

- ScriptedDemandSource: a demand source that yields a programmed
  sequence of DomainDemands (one per contention round).
- GrantSpender: spends a grant's budget and concurrency like a Run
  Controller would -- spend() refuses past the grant (fail-closed),
  proving the grant is a real ceiling, not an advisory number.
"""

from __future__ import annotations

from typing import List, Optional

from swarm_engine.curiosity.frm.policy import DomainDemand
from swarm_engine.curiosity.frm.grant import FrmGrant


class ScriptedDemandSource:
    """Yields a programmed sequence of demands, one per round."""

    def __init__(self, demands: List[DomainDemand]) -> None:
        if not demands:
            raise ValueError("need at least one demand")
        domains = {d.domain for d in demands}
        if len(domains) != 1:
            raise ValueError("one source states demand for one domain")
        self._demands = list(demands)
        self._cursor = 0

    @property
    def domain(self) -> str:
        return self._demands[0].domain

    def next_demand(self) -> DomainDemand:
        d = self._demands[self._cursor]
        if self._cursor < len(self._demands) - 1:
            self._cursor += 1
        return d

    def reset(self) -> None:
        self._cursor = 0


class GrantExhausted(Exception):
    """Raised when a spender tries to exceed its grant."""


class GrantSpender:
    """Spends a real grant like a Run Controller would.

    spend_budget(s) and acquire_slot()/release_slot() track consumption
    against the grant's dimensions. Anything beyond the grant raises
    GrantExhausted -- the grant is enforced, not advisory.
    """

    def __init__(self, grant: FrmGrant) -> None:
        if not isinstance(grant, FrmGrant):
            raise ValueError("spender requires a real FrmGrant")
        self._grant = grant
        self._spent_s = 0.0
        self._slots_held = 0

    @property
    def grant(self) -> FrmGrant:
        return self._grant

    @property
    def spent_s(self) -> float:
        return self._spent_s

    @property
    def slots_held(self) -> int:
        return self._slots_held

    @property
    def remaining_s(self) -> float:
        return self._grant.budget_s - self._spent_s

    def spend_budget(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("cannot spend negative budget")
        if self._spent_s + seconds > self._grant.budget_s + 1e-9:
            raise GrantExhausted(
                f"spend of {seconds:.1f}s would exceed grant "
                f"{self._grant.budget_s:.1f}s "
                f"(already spent {self._spent_s:.1f}s)")
        self._spent_s += seconds
        return self.remaining_s

    def acquire_slot(self) -> int:
        if self._slots_held + 1 > self._grant.max_concurrent:
            raise GrantExhausted(
                f"slot acquisition would exceed grant "
                f"{self._grant.max_concurrent} "
                f"(holding {self._slots_held})")
        self._slots_held += 1
        return self._slots_held

    def release_slot(self) -> int:
        if self._slots_held <= 0:
            raise ValueError("no slots held")
        self._slots_held -= 1
        return self._slots_held
