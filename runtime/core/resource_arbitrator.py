"""runtime/core/resource_arbitrator.py

Resource arbitration for the Primary executive's unified loop.

Charter distinction (C-4.4), enforced by construction:
  ARBITRATION = how much resource a loop is GRANTED (executive-level).
  CAPS       = how much a single activation may CONSUME (enforced within a grant).

This module does arbitration. It sets each loop's resource pool (the grant).
Per-activation caps are enforced by the MicrocontrollerSubstrate's
LoopAdmission pools: spawn() refuses against the pool, so a grant is a
ceiling no activation can silently exceed.

This is NOT the task-tier router in arbitration.py (LocalArbitrator:
DIRECT / SYNTHESIZE / DECOMPOSE -- "what does the engine itself need to do
to handle this task"). That module routes tasks; this one grants resources
between loop controllers. Different decisions, different names, no overlap:
there is exactly one decider per decision.

Ownership: the Run Controller owns resource+concurrency admission (James's
architecture decision). The arbitrator is the mechanism it uses: the Run
Controller (or the executive, during the transition) calls
register_loop / set_demand / arbitrate / apply each cadence tick.

Cooperative philosophy (matches the substrate): grants bound what may
START. Shrinking a grant never kills running work -- the pool's available
balance clamps at zero and new spawns are refused (fail-closed). Breach
handling is refusal-as-a-value, never a silent over-grant.

Loop names are NOT validated against the six Primary loops: the loop set
is provisional (charter C-6.1) and a future Curiosity forest will need the
same mechanism. Any non-empty string registers.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoopPolicy:
    """A loop's standing arbitration policy: its priority weight and its
    anti-starvation minimums. Minimums are granted (scaled if necessary)
    to every loop that states demand; they are never silently dropped."""
    loop: str
    weight: float = 1.0
    minimum_budget_s: float = 0.0
    minimum_concurrent: int = 0


@dataclass(frozen=True)
class LoopDemand:
    """What a loop wants for the coming epoch. Zero demand => zero grant;
    a loop that needs resources states demand at the next arbitration."""
    loop: str
    budget_s: float = 0.0
    concurrent: int = 0


@dataclass(frozen=True)
class ResourceGrant:
    """The arbitrator's decision for one loop, one epoch. A ceiling:
    the substrate refuses spawns beyond it."""
    loop: str
    budget_s: float
    max_concurrent: int
    epoch_id: int
    epoch_deadline: float  # monotonic clock; cooperative re-arbitration signal
    rationale: str = ""


@dataclass
class ArbitrationDecision:
    """One arbitration round, fully inspectable. The ledger keeps the
    recent history so a gate can audit who got what and why."""
    epoch_id: int
    decided_at: float
    grants: Dict[str, ResourceGrant] = field(default_factory=dict)
    total_budget_s: float = 0.0
    total_max_concurrent: int = 0
    headroom_budget_s: float = 0.0
    headroom_concurrent: int = 0
    minimums_scaled: bool = False
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "epoch_id": self.epoch_id,
            "decided_at": self.decided_at,
            "grants": {loop: {"budget_s": g.budget_s,
                              "max_concurrent": g.max_concurrent,
                              "epoch_deadline": g.epoch_deadline,
                              "rationale": g.rationale}
                       for loop, g in self.grants.items()},
            "total_budget_s": self.total_budget_s,
            "total_max_concurrent": self.total_max_concurrent,
            "headroom_budget_s": self.headroom_budget_s,
            "headroom_concurrent": self.headroom_concurrent,
            "minimums_scaled": self.minimums_scaled,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Arbitrator
# ---------------------------------------------------------------------------

class ResourceArbitrator:
    """Decides resource grants between loop controllers under scarcity.

    Policy (fixed, auditable):
      1. Every loop with demand gets its anti-starvation minimum first
         (clamped to its demand). If the minimums alone exceed capacity,
         they are scaled down proportionally and the scaling is RECORDED
         (minimums_scaled=True) -- never silent.
      2. Remaining capacity is distributed by priority weight among loops
         with unmet demand.
      3. No grant exceeds the loop's stated demand (no idle hoarding).
      4. Integer concurrency slots use largest-remainder distribution,
         tie-broken deterministically by (weight desc, loop name).
    """

    def __init__(self, *, total_budget_s: float, total_max_concurrent: int,
                 epoch_length_s: float = 300.0,
                 clock: Callable[[], float] = time.monotonic,
                 ledger_size: int = 64) -> None:
        if not (total_budget_s > 0):
            raise ValueError("total_budget_s must be > 0")
        if not (total_max_concurrent > 0):
            raise ValueError("total_max_concurrent must be > 0")
        if not (epoch_length_s > 0):
            raise ValueError("epoch_length_s must be > 0")
        self._total_budget_s = float(total_budget_s)
        self._total_concurrent = int(total_max_concurrent)
        self._epoch_length_s = float(epoch_length_s)
        self._clock = clock
        self._policies: Dict[str, LoopPolicy] = {}
        self._demands: Dict[str, LoopDemand] = {}
        self._epoch_id = 0
        self._ledger: deque = deque(maxlen=ledger_size)
        self._last: Optional[ArbitrationDecision] = None

    # -- configuration ----------------------------------------------------

    def register_loop(self, loop: str, *, weight: float = 1.0,
                      minimum_budget_s: float = 0.0,
                      minimum_concurrent: int = 0) -> None:
        if not isinstance(loop, str) or not loop.strip():
            raise ValueError("loop must be a non-empty string")
        if not (weight > 0):
            raise ValueError("weight must be > 0")
        if minimum_budget_s < 0 or minimum_concurrent < 0:
            raise ValueError("minimums must be >= 0")
        self._policies[loop] = LoopPolicy(
            loop=loop, weight=float(weight),
            minimum_budget_s=float(minimum_budget_s),
            minimum_concurrent=int(minimum_concurrent))

    def set_demand(self, loop: str, *, budget_s: float = 0.0,
                   concurrent: int = 0) -> None:
        if loop not in self._policies:
            raise ValueError(f"loop {loop!r} not registered with the arbitrator")
        if budget_s < 0 or concurrent < 0:
            raise ValueError("demand must be >= 0")
        self._demands[loop] = LoopDemand(loop=loop, budget_s=float(budget_s),
                                         concurrent=int(concurrent))

    def clear_demands(self) -> None:
        self._demands = {}

    # -- arbitration ------------------------------------------------------

    def arbitrate(self) -> ArbitrationDecision:
        now = self._clock()
        self._epoch_id += 1
        notes: List[str] = []
        demanding = {loop: d for loop, d in self._demands.items()
                     if d.budget_s > 0 or d.concurrent > 0}

        # Pass 1: anti-starvation minimums, clamped to demand.
        base_b: Dict[str, float] = {}
        base_c: Dict[str, int] = {}
        for loop, d in demanding.items():
            p = self._policies[loop]
            base_b[loop] = min(p.minimum_budget_s, d.budget_s)
            base_c[loop] = min(p.minimum_concurrent, d.concurrent)

        minimums_scaled = False
        sum_b = sum(base_b.values())
        sum_c = sum(base_c.values())
        if (sum_b > self._total_budget_s or sum_c > self._total_concurrent
                ) and (sum_b > 0 or sum_c > 0):
            # Configuration asks for more guaranteed minimum than exists.
            # Scale down proportionally and RECORD it -- fail-closed, explicit.
            scale = min(self._total_budget_s / sum_b if sum_b > 0 else 1.0,
                        self._total_concurrent / sum_c if sum_c > 0 else 1.0)
            base_b = {loop: v * scale for loop, v in base_b.items()}
            base_c = {loop: int(v * scale) for loop, v in base_c.items()}
            minimums_scaled = True
            notes.append(
                f"minimums exceed capacity: scaled by {scale:.3f} "
                f"(sum minimums {sum_b:.1f}s/{sum_c} slots vs capacity "
                f"{self._total_budget_s:.1f}s/{self._total_concurrent} slots)")

        # Pass 2: distribute the remainder by weight over unmet demand.
        rem_b = self._total_budget_s - sum(base_b.values())
        rem_c = self._total_concurrent - sum(base_c.values())
        grant_b = dict(base_b)
        grant_c = dict(base_c)

        unmet_b = {loop: demanding[loop].budget_s - base_b[loop]
                   for loop in demanding if demanding[loop].budget_s > base_b[loop]}
        if unmet_b and rem_b > 0:
            wsum = sum(self._policies[loop].weight for loop in unmet_b)
            for loop in unmet_b:
                share = rem_b * self._policies[loop].weight / wsum
                grant_b[loop] = base_b[loop] + min(share, unmet_b[loop])

        unmet_c = {loop: demanding[loop].concurrent - base_c[loop]
                   for loop in demanding if demanding[loop].concurrent > base_c[loop]}
        if unmet_c and rem_c > 0:
            # Largest-remainder over integer slots, deterministic tie-break.
            wsum = sum(self._policies[loop].weight for loop in unmet_c)
            floats = {loop: rem_c * self._policies[loop].weight / wsum
                      for loop in unmet_c}
            floors = {loop: int(f) for loop, f in floats.items()}
            for loop in unmet_c:
                grant_c[loop] = base_c[loop] + min(
                    floors[loop], unmet_c[loop])
            leftover = rem_c - sum(floors.values())
            remainders = sorted(
                unmet_c,
                key=lambda loop: (-(floats[loop] - floors[loop]),
                                 -self._policies[loop].weight, loop))
            for loop in remainders:
                if leftover <= 0:
                    break
                if grant_c[loop] - base_c[loop] < unmet_c[loop]:
                    grant_c[loop] += 1
                    leftover -= 1

        deadline = now + self._epoch_length_s
        grants: Dict[str, ResourceGrant] = {}
        for loop in demanding:
            p = self._policies[loop]
            grants[loop] = ResourceGrant(
                loop=loop,
                budget_s=round(grant_b[loop], 6),
                max_concurrent=int(grant_c[loop]),
                epoch_id=self._epoch_id,
                epoch_deadline=deadline,
                rationale=(f"demand {demanding[loop].budget_s:.1f}s/"
                           f"{demanding[loop].concurrent} slots, weight "
                           f"{p.weight:g} -> grant "
                           f"{grant_b[loop]:.1f}s/{int(grant_c[loop])} slots"))

        decision = ArbitrationDecision(
            epoch_id=self._epoch_id,
            decided_at=now,
            grants=grants,
            total_budget_s=self._total_budget_s,
            total_max_concurrent=self._total_concurrent,
            headroom_budget_s=round(
                self._total_budget_s - sum(g.budget_s for g in grants.values()), 6),
            headroom_concurrent=(
                self._total_concurrent
                - sum(g.max_concurrent for g in grants.values())),
            minimums_scaled=minimums_scaled,
            notes=notes)
        self._ledger.append(decision)
        self._last = decision
        return decision

    def apply(self, substrate) -> ArbitrationDecision:
        """Arbitrate and write the grants into the substrate's per-loop
        pools. Loops the arbitrator does not know are left untouched.
        A loop the arbitrator knows but the substrate has not registered
        is a loud configuration error, not a silent skip."""
        decision = self.arbitrate()
        for loop, grant in decision.grants.items():
            try:
                substrate.set_grant(loop, budget_s=grant.budget_s,
                                    max_concurrent=grant.max_concurrent)
            except KeyError:
                raise KeyError(
                    f"arbitrator granted {loop!r} but it is not registered "
                    f"on the substrate: register the loop on the substrate "
                    f"before applying grants")
        return decision

    # -- cooperative epoch signal -----------------------------------------

    def expired_loops(self) -> List[str]:
        """Loops whose grant epoch has passed on the arbitrator's clock.
        Cooperative: the owner re-arbitrates; nothing is revoked here."""
        if self._last is None:
            return []
        now = self._clock()
        return [loop for loop, g in self._last.grants.items()
                if g.epoch_deadline <= now]

    # -- inspection --------------------------------------------------------

    @property
    def last_decision(self) -> Optional[ArbitrationDecision]:
        return self._last

    @property
    def ledger(self) -> List[ArbitrationDecision]:
        return list(self._ledger)

    @property
    def epoch_id(self) -> int:
        return self._epoch_id
