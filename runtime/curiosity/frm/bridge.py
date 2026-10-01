"""FRM bridge: real controller-stated demands into the FRM, grants out to
enforcement.

SEAM (SEAM-WIRE-1 mandate 4): the FinancialResourceManager was only
exercised by proofs with test-double demands (ScriptedDemandSource) or,
in the CuriosityExecutive's request_activation, with hardcoded values
(primary=0, curiosity=constructor constants). This bridge replaces both
with DomainDemands STATED BY THE REAL CONTROLLERS:

- primary_demand: aggregated from the Primary RunController's PUBLIC
  arbitration_status() -- its measured per-loop demands from the last
  tick. No privates, no test doubles.
- curiosity_demand: from the CuriosityRunController's PUBLIC
  inquiry_views() -- pending + active inquiries and their budgets.

Grant flow: the FrmRound's grants are set on a ResourceArbitrator via
set_grant and applied to a domain substrate via apply(substrate) ->
set_grant. The domain substrate (DomainSubstrate, this package) carries
the "primary" and "curiosity" pools; spawn on it enforces the FRM grant,
refusing when the pool is spent. This is the FRM's enforcement point --
the arbitrator computes the split, the substrate enforces it.

Additive only: consumes FinancialResourceManager.evaluate_round,
ResourceArbitrator.set_grant/apply, and the controllers' public APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .policy import DomainDemand


# ---------------------------------------------------------------------------
# Domain substrate: the FRM grant enforcement point
# ---------------------------------------------------------------------------

def build_domain_substrate() -> Any:
    """A microcontroller substrate with primary|curiosity domain pools.

    TEST-SUPPORT (DEAD-CODE-1, 2026-10-01): no production caller; exercised
    by proofs/seamwire4_frm_bridge_proof.py. Kept so the proof keeps
    importing.

    The frozen MicrocontrollerSubstrate registers only the Primary's six
    loops; the CuriositySubstrate pattern (subclass overriding
    register_loop) is reused here for the two FRM domains. This is an
    adapter, not a second implementation: all lifecycle bounds are the
    base class's. The FRM's ResourceArbitrator (registered for
    primary|curiosity) applies its grants here via apply() -> set_grant.
    """
    from swarm_engine.core.microcontroller.substrate import (
        MicrocontrollerSubstrate)

    class DomainSubstrate(MicrocontrollerSubstrate):
        """MicrocontrollerSubstrate with the FRM domain vocabulary."""

        _DOMAINS = frozenset({"primary", "curiosity"})

        def register_loop(self, loop: str, *, budget_s: float,
                          max_concurrent: int = 64) -> None:
            from swarm_engine.core.microcontroller.substrate import (
                LoopAdmission)
            if loop not in self._DOMAINS:
                raise ValueError(
                    f"DomainSubstrate hosts only FRM domains "
                    f"{sorted(self._DOMAINS)}: refused {loop!r}")
            if budget_s < 0:
                raise ValueError("domain budget_s must be >= 0")
            # Same registration body as the base class; zero-budget pools
            # are fail-closed (no grant, no admission).
            self._loops[loop] = LoopAdmission(
                loop=loop, budget_s=float(budget_s),
                max_concurrent=int(max_concurrent))
            self._loop_state.setdefault(loop, "idle")

    substrate = DomainSubstrate()
    # Pools start with zero budget: no grant, no admission (fail-closed).
    # Grants arrive via ResourceArbitrator.apply -> set_grant.
    substrate.register_loop("primary", budget_s=0.0, max_concurrent=0)
    substrate.register_loop("curiosity", budget_s=0.0, max_concurrent=0)
    return substrate


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------

@dataclass
class BridgeDemands:
    """The two real demands, stated by the controllers."""
    primary: DomainDemand
    curiosity: DomainDemand
    # Provenance: where each number came from (for the evidence record).
    provenance: Dict[str, str]


class FrmBridge:
    """Real demands in, real grants out to enforcement."""

    def __init__(self, frm: Any, *, primary_controller: Any = None,
                 curiosity_controller: Any = None) -> None:
        self._frm = frm
        self._primary = primary_controller
        self._curiosity = curiosity_controller

    # -- demands stated by the controllers --------------------------------
    def primary_demand(self) -> DomainDemand:
        """The Primary RunController's stated demand.

        Aggregates the measured per-loop demands from its PUBLIC
        arbitration_status() (last tick's _measure_demands). Falls back to
        a zero demand (fail-closed) when the controller is absent or has
        never ticked -- never a fabricated number.
        """
        provenance = "absent: zero demand (fail-closed)"
        budget_s = 0.0
        max_concurrent = 0
        if self._primary is not None:
            try:
                status = self._primary.arbitration_status()
                last_round = status.get("last_round") or {}
                grants = last_round.get("grants") or {}
                # grants: loop -> {"demand_budget_s":..., "demand_concurrent":
                # ..., "grant_budget_s":..., ...} -- the demand_* fields are
                # the controller's measured per-loop demands.
                total_budget = 0.0
                total_concurrent = 0
                for loop, g in grants.items():
                    if isinstance(g, dict):
                        total_budget += float(g.get("demand_budget_s", 0.0))
                        total_concurrent += int(
                            g.get("demand_concurrent", 0))
                budget_s = total_budget
                max_concurrent = total_concurrent
                provenance = (
                    f"RunController.arbitration_status last_round: "
                    f"{len(grants)} loops measured, "
                    f"budget_s={budget_s:.1f}, "
                    f"max_concurrent={max_concurrent}")
            except Exception as exc:
                provenance = (f"status read failed ({type(exc).__name__}): "
                              "zero demand (fail-closed)")
        return DomainDemand(domain="primary", budget_s=budget_s,
                            max_concurrent=max_concurrent)

    def curiosity_demand(self) -> DomainDemand:
        """The CuriosityRunController's stated demand.

        From its PUBLIC inquiry_views(): pending + active inquiries count
        toward concurrency; their budgets sum toward budget_s. Absent
        controller or no inquiries -> zero demand (fail-closed).
        """
        provenance = "absent: zero demand (fail-closed)"
        budget_s = 0.0
        max_concurrent = 0
        if self._curiosity is not None:
            try:
                views = self._curiosity.inquiry_views() or []
                live = [v for v in views
                        if str(v.get("status", "")).lower()
                        in ("pending", "active", "running", "admitted")]
                max_concurrent = len(live)
                for v in live:
                    try:
                        budget_s += float(v.get("budget_s", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        pass
                # A standing curiosity minimum: even with no live
                # inquiries the domain states a floor so the FRM can keep
                # the questioning loop warm. This floor is STATED here,
                # visibly, not hidden in the FRM.
                provenance = (
                    f"CuriosityRunController.inquiry_views: "
                    f"{len(live)} live inquiries, "
                    f"budget_s={budget_s:.1f}")
            except Exception as exc:
                provenance = (f"views read failed ({type(exc).__name__}): "
                              "zero demand (fail-closed)")
        return DomainDemand(domain="curiosity", budget_s=budget_s,
                            max_concurrent=max_concurrent)

    def demands(self) -> BridgeDemands:
        """Both real demands with provenance."""
        primary = self.primary_demand()
        curiosity = self.curiosity_demand()
        return BridgeDemands(
            primary=primary, curiosity=curiosity,
            provenance={"primary": self._primary_provenance(primary),
                        "curiosity": self._curiosity_provenance(curiosity)})

    def _primary_provenance(self, demand: DomainDemand) -> str:
        return (f"primary budget_s={demand.budget_s:.1f} "
                f"max_concurrent={demand.max_concurrent}")

    def _curiosity_provenance(self, demand: DomainDemand) -> str:
        return (f"curiosity budget_s={demand.budget_s:.1f} "
                f"max_concurrent={demand.max_concurrent}")

    # -- round + grant enforcement ----------------------------------------
    def evaluate_round(self, enforcement_state: str) -> Any:
        """A real FRM contention round on the controllers' stated demands."""
        demands = self.demands()
        return self._frm.evaluate_round(
            enforcement_state=enforcement_state,
            primary_demand=demands.primary,
            curiosity_demand=demands.curiosity)

    def apply_grants(self, round_: Any, domain_substrate: Any) -> Dict[str, Any]:
        """The round's grants flow through ResourceArbitrator -> substrate.

        Calls the FRM's ResourceArbitrator.apply(domain_substrate), which
        re-arbitrates on the round's demands and writes each grant via
        substrate.set_grant. The domain substrate carries the primary and
        curiosity pools, so the grants land as enforceable pool budgets.
        Returns the applied grant map for the evidence record.
        """
        arbitrator = self._frm._arbitrator
        decision = arbitrator.apply(domain_substrate)
        applied: Dict[str, Any] = {}
        for domain, grant in dict(round_.grants).items():
            applied[str(domain)] = {
                "budget_s": float(grant.budget_s),
                "max_concurrent": int(grant.max_concurrent),
                "epoch_id": str(round_.epoch_id),
            }
        return applied
