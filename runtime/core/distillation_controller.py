"""DIS-CTRL-1: the Distillation Controller as a runtime entity.

James's three-level hierarchy (2026-09-28, standing), level 2. This
controller owns the Distillation loop's convergence process:

    delta -> distill -> verify -> promote, recursive

A promoted technique becomes a primitive eligible for the next
distillation. The recursion M2 seeded but left undriven ("recursive
closure seeded but undriven", 2026-09-27) is now owned by a runtime
entity -- this file.

Inventory-first (James's no-duplication mandate, standing, sharpened by
the 2026-09-28 re-scope): this file builds ONLY the missing wiring.
Everything else is SUBORDINATED -- called through frozen interfaces,
never reimplemented:

  - DistillationLoop.distill()/generalize()
    (swarm_engine/acquisition/distill.py, M2/Q6/Q11-R1): the loop itself.
    Route A (near-miss adaptation), Route B (fresh synthesis + held-out
    verification in fresh processes + negative controls + ReviewBoard +
    verdict-bound promotion). Called, never edited.
  - run_distillation_sweep / find_pending_deltas / mark_consumed
    (swarm_engine/acquisition/distill_driver.py, V10-P4): the convergence
    step -- pending-delta intake, distill, verify, promote, append-only
    consumption marking. The sweep IS the cycle's work; this controller
    is its owner. Called, never edited.
  - DeltaSession / delta_capture.py (V10-P3): the only delta source.
  - record_experience / read_experiences
    (swarm_engine/intellect/unified_memory.py, V10-P1): the single
    read/write path.
  - ReviewBoard + VerdictPromotionBridge + AcquiredCodeStore: the trust
    path. This controller introduces no new trust code and no second
    verifier (the driver's anti-duplication contract stands).
  - MicrocontrollerSubstrate (swarm_engine/core/microcontroller/,
    RUN-MICRO-1, frozen at microcontroller-interface/v1): the level-3
    lifecycle machinery. This controller hosts distillation-graph
    microcontrollers through it -- it does not reimplement spawn/retire,
    depth caps, or budget caps.

What this file ADDS (the genuinely missing wiring):

  1. A runtime entity with identity: DistillationController(engine,
     epistemic, substrate=None). The loop's convergence has an owner;
     before this file it was a module-level function with no state.
  2. A convergence state machine: idle / active / resolved. cycle()
     drives one convergence step; the controller tracks cycles completed,
     the last cycle's outcome, and consecutive idle cycles.
  3. Ownership of the recursive feed-through: distillation_scope()
     reports the primitive names eligible for the next distillation,
     drawn from the live primitive registry -- the SAME registry that
     distill()'s Route A draws on (adapt_compatible(..., registry=
     self.primitives), core/engine.py adapt_capability) and Route B draws
     on (synthesize_and_admit -> cognition.propose_multi, which searches
     over the registry the CognitiveEngine was constructed with,
     core/engine.py:289). The verdict-promotion bridge registers promoted
     techniques there as family "acquired"
     (capability/verdict_promotion.py: promote), and boot-time
     rehydrate_acquired re-derives the verdict and re-promotes them
     (core/engine.py:555,1067). The controller makes that eligibility
     visible per cycle and asserts the invariant -- it does not rebuild
     the mechanism.
  4. An executive-visible status surface: status() carries loop-level
     fields only. No microcontroller ids, purposes, or internals cross
     above the loop level -- the frozen interface's encapsulation rule
     (LoopView is the only type the executive may consume; this
     controller's status() honors the same rule by construction).
  5. The microcontroller inlet: spawn_microcontroller /
     retire_microcontroller / loop_view, delegating to the shared
     substrate with loop="distillation". Retirement unwinds TO this
     controller: retire_microcontroller is the only path this loop's
     callers use, and resolve() cascade-retires stragglers through the
     substrate before marking the loop resolved -- the stack unwinds
     bottom-up to the controller, which resolves to the executive.

Run Controller relationship (standing architectural decision,
2026-09-27): the Run Controller owns cadence and execution control; the
scheduler is subordinate infrastructure. This controller is INVOCABLE on
that cadence: cycle(limit=...) mirrors the sweep's calling convention
and returns the same summary shape the RunController's tick step 2
consumes (examined/distilled/refused/errors), so the tick can delegate
its _distill_sweep to DistillationController.cycle() with a one-line
change. That wiring edit belongs to the Run track (RUN-EXEC-1 owns the
run layer); this file declares the seam and does not touch
run_controller.py. Idempotency per cycle is inherited: the existing
append-only consumption records guarantee no double-consumption, and
this controller reuses them (DIS-SWEEP-1, parked, later makes the sweep
the standing cycle under Run cadence -- this mission establishes the
entity that will own it).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# Level-2 loop identity, matching the frozen level-3 interface's loop set
# (swarm_engine/core/microcontroller/substrate.py: LOOP_DISTILLATION).
LOOP_ID = "distillation"

# States of the convergence state machine. "resolved" is terminal and
# named: the controller records WHY it resolved.
STATE_IDLE = "idle"
STATE_ACTIVE = "active"
STATE_RESOLVED = "resolved"

# This controller's per-loop admission pool request on the shared
# substrate. Subordinate to the RunController's run-level admission --
# never a second run-level authority. (RUN-MICRO-1's substrate enforces
# the pool; this controller only declares its ask.)
_DEFAULT_LOOP_BUDGET_S = 3600.0
_DEFAULT_MAX_CONCURRENT = 16

# Keys the executive-facing status() may carry. Anything else --
# microcontroller ids, purposes, graphs -- stays below the loop level.
_STATUS_KEYS = frozenset({
    "loop", "state", "cycles_completed", "consecutive_idle_cycles",
    "last_cycle", "resolution", "microcontrollers",
})


def _load_substrate_types():
    """Import the frozen level-3 interface lazily.

    The substrate is the Run track's in-flight work; importing it lazily
    keeps this controller importable (and its core cycle runnable) even
    while that tree is mid-edit. Returns (module, error-or-None).
    """
    try:
        from swarm_engine.core import microcontroller as mc
        return mc, None
    except Exception as exc:  # noqa: BLE001 -- named, never silent
        return None, f"{type(exc).__name__}: {exc}"


class DistillationController:
    """Owns the Distillation loop's convergence: delta -> distill ->
    verify -> promote, recursive.

    engine: the SwarmEngine (or compatible) -- provides .primitives
        (the registry the next distillation draws on), and is passed
        through to the subordinated sweep/loop machinery.
    epistemic: the epistemic store -- the sweep's intake reads pending
        technique deltas through it. Required for cycle(); without it the
        cycle refuses with a named reason (intake is impossible).
    substrate: the shared MicrocontrollerSubstrate (owned by the Run
        track). There is exactly one substrate; this controller never
        constructs its own -- a private substrate would be a second
        admission authority. Without one, the microcontroller inlet
        refuses with a named reason while the core cycle still runs.
    """

    def __init__(self, engine: Any, epistemic: Any = None,
                 substrate: Any = None) -> None:
        self.engine = engine
        self.epistemic = epistemic
        self.substrate = substrate
        self._state = STATE_IDLE
        self._cycles = 0
        self._idle_streak = 0
        self._last_summary: Optional[Dict[str, Any]] = None
        self._resolution: Optional[str] = None
        self._born_at = time.time()
        self._mc_refusals = 0  # inlet-level refusals observed here
        if substrate is not None:
            self._ensure_loop_registered(substrate)

    # ------------------------------------------------------------------
    # Convergence: one owned cycle
    # ------------------------------------------------------------------

    def cycle(self, limit: int = 1000) -> Dict[str, Any]:
        """Drive one convergence step: intake -> distill -> verify ->
        promote, over the subordinated V10-P4 sweep.

        Calling convention and summary shape mirror run_distillation_sweep
        so the RunController's tick can delegate its distillation step
        here (the wiring belongs to the Run track). Idempotent per cycle:
        the sweep's append-only consumption records are reused, so a
        second cycle examines 0 deltas.
        """
        if self._state == STATE_RESOLVED:
            return {"cycle_refused": f"controller resolved: {self._resolution}",
                    "controller": LOOP_ID, "cycle": self._cycles}
        if self.epistemic is None:
            return {"cycle_refused":
                    "no epistemic store attached: delta intake impossible",
                    "controller": LOOP_ID, "cycle": self._cycles}
        from swarm_engine.acquisition.distill_driver import (
            run_distillation_sweep)
        self._state = STATE_ACTIVE
        self._cycles += 1
        try:
            summary = run_distillation_sweep(
                self.engine, self.epistemic, limit=limit)
        except Exception as exc:  # noqa: BLE001 -- recorded, never raised
            summary = {"examined": 0, "distilled": [], "refused": [],
                       "errors": [f"cycle: {type(exc).__name__}: {exc}"]}
        summary["controller"] = LOOP_ID
        summary["cycle"] = self._cycles
        self._last_summary = summary
        if summary.get("examined"):
            self._idle_streak = 0
        else:
            self._idle_streak += 1
        self._recompute_state()
        return summary

    def _recompute_state(self) -> None:
        """idle iff nothing is pending and no microcontrollers are active;
        otherwise active. Resolved is terminal and never recomputed away."""
        if self._state == STATE_RESOLVED:
            return
        try:
            from swarm_engine.acquisition.distill_driver import (
                find_pending_deltas)
            pending = find_pending_deltas(self.epistemic, limit=1)
        except Exception:
            pending = [{"unknown": True}]  # fail active, never fail idle
        active_mcs = 0
        if self.substrate is not None:
            try:
                view = self.substrate.loop_view(LOOP_ID)
                active_mcs = view.active_count
            except Exception:
                active_mcs = 0
        self._state = (STATE_ACTIVE if (pending or active_mcs)
                       else STATE_IDLE)

    # ------------------------------------------------------------------
    # Recursive feed-through: what the NEXT distillation can draw on
    # ------------------------------------------------------------------

    def distillation_scope(self) -> Dict[str, Any]:
        """The primitive names eligible for the next distillation cycle.

        Drawn from the live primitive registry -- the same registry
        distill()'s Route A (adapt_compatible, registry=self.primitives)
        and Route B (cognition.propose_multi over the registry) draw on.
        A technique promoted through the verdict-promotion bridge is
        registered here as family "acquired" and restored here at boot by
        rehydrate_acquired; this method makes that eligibility visible and
        owned. A promoted name present here in a fresh process is the
        structural half of the recursive closure (the full second-order
        distillation proof is DIS-REC-1's work, parked behind this
        mission).
        """
        names: List[str] = []
        families: Dict[str, List[str]] = {}
        try:
            names = list(self.engine.primitives.names())
            families = {f: list(v) for f, v in
                        self.engine.primitives.families().items()}
        except Exception as exc:  # noqa: BLE001 -- scope degrades honestly
            return {"eligible_primitives": [], "families": {},
                    "count": 0, "error": f"{type(exc).__name__}: {exc}"}
        acquired = families.get("acquired", [])
        promoted = families.get("promoted", [])
        return {"eligible_primitives": names,
                "families": families,
                "acquired": acquired,
                "promoted": promoted,
                "count": len(names)}

    # ------------------------------------------------------------------
    # Executive-visible status: loop level only
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """What the executive may see. Loop-level fields only -- no
        microcontroller ids, purposes, or internals cross above the loop
        level (the frozen interface's encapsulation rule, honored here by
        construction: only aggregate counts leave the loop).
        """
        mc_agg = {"active": 0, "spawned": 0, "retired": 0, "refused": 0}
        if self.substrate is not None:
            try:
                view = self.substrate.loop_view(LOOP_ID)
                mc_agg = {"active": view.active_count,
                          "spawned": view.total_spawned,
                          "retired": view.total_retired,
                          "refused": view.total_refused}
            except Exception:
                pass
        mc_agg["refused"] += self._mc_refusals
        last = None
        if self._last_summary is not None:
            s = self._last_summary
            last = {"examined": s.get("examined", 0),
                    "distilled": len(s.get("distilled", [])),
                    "refused": len(s.get("refused", [])),
                    "errors": len(s.get("errors", []))}
        return {"loop": LOOP_ID,
                "state": self._state,
                "cycles_completed": self._cycles,
                "consecutive_idle_cycles": self._idle_streak,
                "last_cycle": last,
                "resolution": ({"reason": self._resolution}
                               if self._resolution else None),
                "microcontrollers": mc_agg}

    # ------------------------------------------------------------------
    # Microcontroller inlet (frozen interface microcontroller-interface/v1)
    # ------------------------------------------------------------------

    def _ensure_loop_registered(self, substrate: Any) -> None:
        """Register loop "distillation" on the shared substrate, once."""
        try:
            substrate.loop_view(LOOP_ID)
            return  # already registered: never reset another owner's pool
        except Exception:
            pass
        substrate.register_loop(LOOP_ID, budget_s=_DEFAULT_LOOP_BUDGET_S,
                                max_concurrent=_DEFAULT_MAX_CONCURRENT)

    def _require_substrate(self) -> Any:
        if self.substrate is None:
            raise RuntimeError(
                "microcontroller inlet unavailable: no substrate attached; "
                "the shared MicrocontrollerSubstrate is owned by the Run "
                "track (RUN-MICRO-1) and handed to this controller at "
                "construction -- this controller never builds its own")
        return self.substrate

    def spawn_microcontroller(self, purpose: str, *,
                              parent_id: Optional[str] = None,
                              budget_s: float,
                              max_children: int = 8):
        """Host one bounded unit of distillation-graph work.

        Delegates to the shared substrate with loop="distillation" --
        the frozen interface's spawn() verbatim. Returns the frozen
        SpawnResult (ok / mc / refusal-as-value). Cross-loop spawning is
        impossible by construction: the loop is fixed to this
        controller's own.
        """
        try:
            substrate = self._require_substrate()
        except RuntimeError as exc:
            self._mc_refusals += 1
            mc_mod, _err = _load_substrate_types()
            if mc_mod is not None:
                return mc_mod.SpawnResult(
                    ok=False, refusal=mc_mod.Refusal(
                        reason="substrate_unattached", message=str(exc)))
            return {"ok": False, "refusal": {"reason": "substrate_unattached",
                                             "message": str(exc)}}
        return substrate.spawn(LOOP_ID, purpose, parent_id=parent_id,
                               budget_s=budget_s, max_children=max_children)

    def retire_microcontroller(self, mc_id: str,
                               outcome: str = "resolved"):
        """Retire one of this loop's microcontrollers.

        Delegates to the shared substrate with loop="distillation", so a
        retire claiming any other loop is refused cross_loop by the
        substrate itself. Retirement unwinds TO this controller: this is
        the only path this loop's callers use, and resolve() funnels the
        whole stack through here. Returns the retired Microcontroller
        record or a Refusal value (never raises on a refused retire).
        """
        substrate = self._require_substrate()
        return substrate.retire(mc_id, outcome, loop=LOOP_ID)

    def charge_microcontroller(self, mc_id: str, seconds: float):
        """Cooperative budget charging, straight through to the substrate.
        Returns (exhausted: bool, state: str)."""
        substrate = self._require_substrate()
        return substrate.charge(mc_id, seconds)

    def loop_view(self):
        """The executive-facing view of this loop's microcontroller
        population. When a substrate is attached this IS the frozen
        LoopView (aggregates only -- no ids, purposes, or internals, by
        construction). Without a substrate, an equivalent empty view with
        this controller's own convergence state.
        """
        mc_mod, _err = _load_substrate_types()
        if self.substrate is not None and mc_mod is not None:
            try:
                return self.substrate.loop_view(LOOP_ID)
            except Exception:
                pass
        if mc_mod is not None:
            return mc_mod.LoopView(loop=LOOP_ID, state=self._state,
                                   active_count=0, total_spawned=0,
                                   total_retired=0, total_refused=0)
        return {"loop": LOOP_ID, "state": self._state, "active_count": 0,
                "total_spawned": 0, "total_retired": 0, "total_refused": 0}

    # ------------------------------------------------------------------
    # Resolution: the loop controller resolves to the executive
    # ------------------------------------------------------------------

    def resolve(self, reason: str = "controller resolved") -> Dict[str, Any]:
        """Resolve the loop: cascade-retire any straggler microcontrollers
        through the substrate (the unwind bottoms out here), then mark the
        loop resolved with the named reason. Terminal: further cycle()
        calls refuse with the reason. The executive sees the resolution in
        status()/loop_view().
        """
        retired_stragglers = 0
        if self.substrate is not None:
            try:
                before = self.substrate.loop_view(LOOP_ID).active_count
                self.substrate.resolve_loop(LOOP_ID)
                retired_stragglers = before
            except Exception:
                pass
        self._resolution = str(reason)
        self._state = STATE_RESOLVED
        return {"loop": LOOP_ID, "state": STATE_RESOLVED,
                "reason": self._resolution,
                "stragglers_retired": retired_stragglers}
