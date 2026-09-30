"""RUN-MICRO-1 -- Microcontroller substrate.

James's three-level hierarchy (2026-09-28, standing), level 3: microcontrollers
operate graphs INSIDE a loop controller. A microcontroller may spawn smaller
ones (MC-1 -> MC-2 -> MC-3 ...), all local to the loop; on resolution the
stack unwinds (MC-4 retires -> MC-3 resumes -> ... -> loop controller resolves
-> executive). The executive still just sees "Acquisition is active" -- never
the internal microcontroller population; its view stays O(6).

This module is the bounded lifecycle machinery the recursion needs so it
cannot run away: spawn/retire semantics, depth caps, budget caps
(per-microcontroller, per-loop), and per-loop resource admission.

Load-bearing sentence (James): "Reasoning models provide cognition wherever
a microcontroller requires it" -- external reasoning is a cognition UTILITY,
not an architectural layer. The substrate exposes a cognition inlet
(CognitionProvider protocol). No reasoning model is hard-wired here; the
default provider refuses honestly (cognition_unavailable).

Relationship notes (recorded, not settled -- Q1 ownership is RUN-EXEC-1's):
  - V10-P2 RunController owns cadence/execution control; its run-level
    admission is the PARENT discipline. This substrate's per-loop admission
    is subordinate to it, never a second run-level admission.
  - Q1's CognitionLoop (runtime/acquisition/loop_driver.py) is the continuous
    acquisition driver the future Acquisition loop controller will host
    microcontrollers through. This substrate does not drive it.
  - The executive selection layer (RUN-EXEC-1) consumes LoopView, never
    Microcontroller records -- enforced by construction (see loop_view()).

The interface this module exposes is FROZEN at v1 (see INTERFACE.md).
Breaking changes after freeze require James's explicit decision.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

#: Frozen interface version. The other five tracks code against this stamp.
INTERFACE_VERSION = "microcontroller-interface/v1"

#: The six nested-loop controllers of level 2.
LOOP_RUN = "run"
LOOP_ACQUISITION = "acquisition"
LOOP_EXECUTION = "execution"
LOOP_ACCEPTANCE = "acceptance"
LOOP_DISTILLATION = "distillation"
LOOP_GENERALIZATION = "generalization"
LOOPS = (LOOP_RUN, LOOP_ACQUISITION, LOOP_EXECUTION,
         LOOP_ACCEPTANCE, LOOP_DISTILLATION, LOOP_GENERALIZATION)

#: Default recursion bound. The recursion needs bounds or it runs away.
DEFAULT_MAX_DEPTH = 8
#: Default per-microcontroller child allowance.
DEFAULT_MAX_CHILDREN = 8

# Microcontroller states.
MC_ACTIVE = "active"
MC_RESOLVED = "resolved"
MC_EXHAUSTED = "exhausted"
MC_RETIRED_CASCADE = "retired_cascade"

# Refusal reason codes.
R_DEPTH_CAP = "depth_cap"
R_ADMISSION_EXHAUSTED = "admission_exhausted"
R_CONCURRENCY_CAP = "concurrency_cap"
R_CROSS_LOOP = "cross_loop"
R_UNKNOWN_PARENT = "unknown_parent"
R_UNKNOWN_MC = "unknown_microcontroller"
R_ALREADY_RETIRED = "already_retired"
R_INVALID_PURPOSE = "invalid_purpose"
R_INVALID_BUDGET = "invalid_budget"
R_LOOP_UNREGISTERED = "loop_unregistered"
R_MC_NOT_ACTIVE = "mc_not_active"
R_COGNITION_UNAVAILABLE = "cognition_unavailable"


@dataclass(frozen=True)
class Refusal:
    """A refused spawn/retire/cognize call. Refusals are values, never exceptions."""
    reason: str
    message: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CognitionResult:
    ok: bool
    text: str = ""
    error: str = ""
    # Governance provenance (BRAIN-SCAFFOLD-1, additive): set by the
    # governed provider. "native:<mechanism>" or "borrowed:<model>@<rev>".
    provenance: str = ""
    # The named native refusal that justified a borrow ("", if none).
    native_refusal: str = ""
    # Telemetry correlation id for proposal-survival tracking.
    result_id: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CognitionProvider(Protocol):
    """The cognition inlet. Any reasoning source implements this; the
    substrate never constructs or selects one. External reasoning is a
    utility wherever a microcontroller requires it -- not a layer."""

    def request_cognition(self, *, mc_id: str, prompt: str,
                          context: Dict[str, Any]) -> CognitionResult:
        ...


class NullCognitionProvider:
    """Default provider: refuses honestly. A microcontroller that requires
    reasoning with no provider registered must handle cognition_unavailable
    or fail closed -- never hallucinate a result."""

    def request_cognition(self, *, mc_id: str, prompt: str,
                          context: Dict[str, Any]) -> CognitionResult:
        return CognitionResult(
            ok=False,
            error=f"{R_COGNITION_UNAVAILABLE}: no cognition provider "
                  f"registered for microcontroller {mc_id}")


@dataclass
class Microcontroller:
    """One bounded unit of loop-internal work. Declares its purpose, its
    parent, and its loop at spawn; the substrate enforces the bounds."""
    mc_id: str
    loop: str
    purpose: str
    parent_id: Optional[str]
    depth: int
    state: str = MC_ACTIVE
    budget_s: float = 0.0
    remaining_s: float = 0.0
    max_children: int = DEFAULT_MAX_CHILDREN
    children: List[str] = field(default_factory=list)
    born_at: float = 0.0
    resolved_at: Optional[float] = None
    outcome: Optional[str] = None
    fabrication_flags: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LoopAdmission:
    """The per-loop resource pool. Spawn reserves from it; retire releases
    back. Subordinate to the RunController's run-level admission."""
    loop: str
    budget_s: float
    reserved_s: float = 0.0
    max_concurrent: int = 64
    total_spawned: int = 0
    total_retired: int = 0
    total_refused: int = 0

    @property
    def available_s(self) -> float:
        return max(0.0, self.budget_s - self.reserved_s)


@dataclass(frozen=True)
class LoopView:
    """What anything above the loop level may see. Contains NO
    microcontroller ids, purposes, or internals -- enforced by construction:
    this is the only type the executive layer is allowed to consume."""
    loop: str
    state: str          # "idle" | "active" | "resolved"
    active_count: int
    total_spawned: int
    total_retired: int
    total_refused: int

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SpawnResult:
    ok: bool
    mc: Optional[Microcontroller] = None
    refusal: Optional[Refusal] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok,
                "mc": self.mc.as_dict() if self.mc else None,
                "refusal": self.refusal.as_dict() if self.refusal else None}


class MicrocontrollerSubstrate:
    """The bounded lifecycle machinery for level-3 microcontrollers.

    Cooperative and single-threaded, like the RunController that clocks it:
    budgets are charged between units of work; a unit already running is
    never killed mid-flight -- the bounds only prevent starting new work
    and retire what has overrun.
    """

    def __init__(self, *, max_depth: int = DEFAULT_MAX_DEPTH,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        self.max_depth = max_depth
        self._clock = clock
        self._loops: Dict[str, LoopAdmission] = {}
        self._mcs: Dict[str, Microcontroller] = {}
        self._ledger: List[Microcontroller] = []  # retired records, inspectable
        self._provider: CognitionProvider = NullCognitionProvider()
        self._loop_state: Dict[str, str] = {}  # loop -> "idle"|"active"|"resolved"

    # ------------------------------------------------------------------
    # Loop registration + cognition inlet
    # ------------------------------------------------------------------

    def register_loop(self, loop: str, *, budget_s: float,
                      max_concurrent: int = 64) -> None:
        if loop not in LOOPS:
            raise ValueError(f"unknown loop {loop!r}; expected one of {LOOPS}")
        if budget_s <= 0:
            raise ValueError("loop budget_s must be > 0")
        self._loops[loop] = LoopAdmission(loop=loop, budget_s=float(budget_s),
                                          max_concurrent=int(max_concurrent))
        self._loop_state.setdefault(loop, "idle")

    def set_grant(self, loop: str, *, budget_s: float,
                  max_concurrent: int) -> None:
        """Write a resource-arbitration grant into the loop's pool.

        Owned by the ResourceArbitrator (runtime/core/resource_arbitrator.py):
        the grant is a ceiling on what the loop may start, never a floor.
        Shrinking a grant below already-reserved spend does NOT kill running
        work -- available_s clamps at zero and new spawns are refused
        (fail-closed), matching the substrate's cooperative philosophy.
        Raises KeyError for an unregistered loop: a grant for a loop the
        substrate does not know is a loud configuration error, never a
        silent skip.
        """
        adm = self._loops[loop]  # KeyError if unregistered: intentional
        if budget_s < 0:
            raise ValueError("grant budget_s must be >= 0")
        if max_concurrent < 0:
            raise ValueError("grant max_concurrent must be >= 0")
        adm.budget_s = float(budget_s)
        adm.max_concurrent = int(max_concurrent)

    def set_cognition_provider(self, provider: CognitionProvider) -> None:
        self._provider = provider

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    def spawn(self, loop: str, purpose: str, *,
              parent_id: Optional[str] = None,
              budget_s: float,
              max_children: int = DEFAULT_MAX_CHILDREN) -> SpawnResult:
        now = self._clock()

        def _refuse(reason: str, message: str) -> SpawnResult:
            adm = self._loops.get(loop)
            if adm is not None:
                adm.total_refused += 1
            return SpawnResult(ok=False,
                               refusal=Refusal(reason=reason, message=message))

        if loop not in self._loops:
            return SpawnResult(
                ok=False, refusal=Refusal(
                    reason=R_LOOP_UNREGISTERED,
                    message=f"loop {loop!r} not registered on this substrate"))
        if not isinstance(purpose, str) or not purpose.strip():
            return _refuse(R_INVALID_PURPOSE,
                           "purpose must be a non-empty declared string")
        if not (budget_s > 0):
            return _refuse(R_INVALID_BUDGET, "budget_s must be > 0")
        if max_children < 0:
            return _refuse(R_INVALID_BUDGET, "max_children must be >= 0")

        adm = self._loops[loop]
        parent: Optional[Microcontroller] = None
        depth = 0
        if parent_id is not None:
            parent = self._mcs.get(parent_id)
            if parent is None:
                return _refuse(R_UNKNOWN_PARENT,
                               f"parent {parent_id!r} unknown to this substrate")
            if parent.state != MC_ACTIVE:
                return _refuse(R_UNKNOWN_PARENT,
                               f"parent {parent_id!r} is not active "
                               f"(state={parent.state})")
            if parent.loop != loop:
                return _refuse(R_CROSS_LOOP,
                               f"parent {parent_id!r} lives in loop "
                               f"{parent.loop!r}, cannot spawn into {loop!r}: "
                               "microcontrollers are local to their loop")
            if len(parent.children) >= parent.max_children:
                return _refuse(R_CONCURRENCY_CAP,
                               f"parent {parent_id!r} already has "
                               f"{len(parent.children)} children "
                               f"(max_children={parent.max_children})")
            depth = parent.depth + 1
            if depth > self.max_depth:
                return _refuse(R_DEPTH_CAP,
                               f"spawn would reach depth {depth} > "
                               f"max_depth {self.max_depth}: recursion capped")

        active_here = sum(1 for m in self._mcs.values()
                          if m.loop == loop and m.state == MC_ACTIVE)
        if active_here >= adm.max_concurrent:
            return _refuse(R_CONCURRENCY_CAP,
                           f"loop {loop!r} already has {active_here} active "
                           f"microcontrollers (max_concurrent={adm.max_concurrent})")
        if budget_s > adm.available_s:
            return _refuse(R_ADMISSION_EXHAUSTED,
                           f"loop {loop!r} admission pool has "
                           f"{adm.available_s:.3f}s available, spawn asked "
                           f"{budget_s:.3f}s: refused against the pool")

        mc_id = f"mc-{uuid.uuid4().hex[:12]}"
        mc = Microcontroller(
            mc_id=mc_id, loop=loop, purpose=purpose.strip(),
            parent_id=parent_id, depth=depth,
            budget_s=float(budget_s), remaining_s=float(budget_s),
            max_children=int(max_children), born_at=now)
        self._mcs[mc_id] = mc
        if parent is not None:
            parent.children.append(mc_id)
        adm.reserved_s += float(budget_s)
        adm.total_spawned += 1
        self._loop_state[loop] = "active"
        return SpawnResult(ok=True, mc=mc)

    # ------------------------------------------------------------------
    # Budget charging (cooperative)
    # ------------------------------------------------------------------

    def charge(self, mc_id: str, seconds: float) -> Tuple[bool, str]:
        """Charge wall-clock against a microcontroller. Returns
        (exhausted, state). On exhaustion the microcontroller is marked
        EXHAUSTED and its active children are cascade-retired -- but the
        exhausted record stays in place (reservation held) until its owner
        retires it or the loop resolves. A later 'resolved' claim on it is
        a fabricated retire: detected, flagged, and downgraded."""
        mc = self._mcs.get(mc_id)
        if mc is None:
            return False, R_UNKNOWN_MC
        if mc.state != MC_ACTIVE:
            return False, mc.state
        mc.remaining_s -= float(seconds)
        if mc.remaining_s > 0:
            return False, MC_ACTIVE
        self._cascade_children(mc, outcome=MC_RETIRED_CASCADE)
        mc.state = MC_EXHAUSTED
        mc.outcome = MC_EXHAUSTED
        return True, MC_EXHAUSTED

    # ------------------------------------------------------------------
    # Cognition inlet
    # ------------------------------------------------------------------

    def cognize(self, mc_id: str, prompt: str,
                context: Optional[Dict[str, Any]] = None):
        mc = self._mcs.get(mc_id)
        if mc is None:
            retired = next((r for r in self._ledger if r.mc_id == mc_id), None)
            if retired is not None:
                return CognitionResult(
                    ok=False,
                    error=f"{R_MC_NOT_ACTIVE}: {mc_id!r} retired "
                          f"(outcome={retired.outcome})")
            return CognitionResult(ok=False,
                                   error=f"{R_UNKNOWN_MC}: {mc_id!r}")
        if mc.state != MC_ACTIVE:
            return CognitionResult(
                ok=False,
                error=f"{R_MC_NOT_ACTIVE}: {mc_id!r} is {mc.state}")
        return self._provider.request_cognition(
            mc_id=mc_id, prompt=prompt, context=dict(context or {}))

    # ------------------------------------------------------------------
    # Retire / unwind
    # ------------------------------------------------------------------

    def retire(self, mc_id: str, outcome: str = MC_RESOLVED, *,
               loop: Optional[str] = None):
        """Retire a microcontroller. Children still active are cascade-retired
        first (the unwind: MC-4 retires -> MC-3 resumes -> ...). A 'resolved'
        claim on an exhausted microcontroller is a fabricated retire: it is
        detected, flagged on the record, and the outcome is downgraded."""
        mc = self._mcs.get(mc_id)
        if mc is None:
            # It may already be in the retired ledger.
            if any(r.mc_id == mc_id for r in self._ledger):
                return Refusal(reason=R_ALREADY_RETIRED,
                               message=f"{mc_id!r} already retired")
            return Refusal(reason=R_UNKNOWN_MC,
                           message=f"{mc_id!r} unknown to this substrate")
        if loop is not None and loop != mc.loop:
            return Refusal(reason=R_CROSS_LOOP,
                           message=f"{mc_id!r} lives in loop {mc.loop!r}, "
                                   f"retire claimed loop {loop!r}")
        if mc.state != MC_ACTIVE and mc.state != MC_EXHAUSTED:
            return Refusal(reason=R_ALREADY_RETIRED,
                           message=f"{mc_id!r} already in state {mc.state}")

        # Fabricated-retire detection: resolved claimed on an exhausted mc.
        if mc.state == MC_EXHAUSTED and outcome == MC_RESOLVED:
            mc.fabrication_flags.append("fabricated_resolve_on_exhausted")
            outcome = MC_EXHAUSTED

        # Unwind: cascade-retire active children first, then this mc.
        self._cascade_children(mc, outcome=MC_RETIRED_CASCADE)

        self._release(mc)
        mc.state = outcome if outcome in (MC_RESOLVED, MC_EXHAUSTED) else MC_RESOLVED
        mc.outcome = outcome
        mc.resolved_at = self._clock()
        self._ledger.append(mc)
        del self._mcs[mc_id]
        adm = self._loops[mc.loop]
        adm.total_retired += 1
        if not any(m.loop == mc.loop and m.state == MC_ACTIVE
                   for m in self._mcs.values()):
            self._loop_state[mc.loop] = "idle"
        return mc

    def _cascade_children(self, mc: Microcontroller, outcome: str) -> None:
        """Retire all active children of mc (recursively), releasing their
        reservations and ledgering them. mc itself is left in place."""
        for child_id in list(mc.children):
            child = self._mcs.get(child_id)
            if child is not None and child.state in (MC_ACTIVE, MC_EXHAUSTED):
                self._cascade_children(child, outcome=outcome)
                self._release(child)
                child.state = outcome
                child.outcome = outcome
                child.resolved_at = self._clock()
                self._ledger.append(child)
                del self._mcs[child_id]
                self._loops[child.loop].total_retired += 1

    def _force_retire_subtree(self, mc: Microcontroller, outcome: str) -> None:
        """Cascade-retire mc and its whole subtree (used by resolve_loop)."""
        self._cascade_children(mc, outcome=MC_RETIRED_CASCADE)
        self._release(mc)
        mc.state = outcome
        mc.outcome = outcome
        mc.resolved_at = self._clock()
        self._ledger.append(mc)
        self._mcs.pop(mc.mc_id, None)
        self._loops[mc.loop].total_retired += 1

    def _release(self, mc: Microcontroller) -> None:
        adm = self._loops[mc.loop]
        adm.reserved_s = max(0.0, adm.reserved_s - mc.budget_s)

    # ------------------------------------------------------------------
    # Loop-level resolution + the executive-facing view
    # ------------------------------------------------------------------

    def resolve_loop(self, loop: str) -> LoopView:
        """The loop controller signals convergence: any remaining active
        microcontrollers are cascade-retired, then the loop resolves."""
        if loop not in self._loops:
            raise ValueError(f"loop {loop!r} not registered")
        for mc in [m for m in self._mcs.values()
                   if m.loop == loop and m.state in (MC_ACTIVE, MC_EXHAUSTED)]:
            self._force_retire_subtree(mc, outcome=MC_RETIRED_CASCADE)
        self._loop_state[loop] = "resolved"
        return self.loop_view(loop)

    def loop_view(self, loop: str) -> LoopView:
        """The ONLY type the executive layer may consume. No microcontroller
        ids, purposes, or internals -- enforced by construction."""
        if loop not in self._loops:
            raise ValueError(f"loop {loop!r} not registered")
        adm = self._loops[loop]
        active = sum(1 for m in self._mcs.values()
                     if m.loop == loop and m.state == MC_ACTIVE)
        state = self._loop_state.get(loop, "idle")
        if state == "active" and active == 0:
            state = "idle"
        return LoopView(loop=loop, state=state, active_count=active,
                        total_spawned=adm.total_spawned,
                        total_retired=adm.total_retired,
                        total_refused=adm.total_refused)

    # ------------------------------------------------------------------
    # Checkpoint seam (for the RunController's checkpoint/recovery)
    # ------------------------------------------------------------------

    def export_state(self) -> Dict[str, Any]:
        """JSON-serializable snapshot. The RunController's checkpoint path
        may persist this and restore it via import_state -- the declared seam
        (see SEAM_RUN_LOOP.md); this mission does not wire it."""
        return {
            "interface_version": INTERFACE_VERSION,
            "max_depth": self.max_depth,
            "loops": {k: asdict(v) for k, v in self._loops.items()},
            "loop_state": dict(self._loop_state),
            "active": [m.as_dict() for m in self._mcs.values()],
            "ledger": [m.as_dict() for m in self._ledger],
        }

    def import_state(self, snapshot: Dict[str, Any]) -> None:
        if snapshot.get("interface_version") != INTERFACE_VERSION:
            raise ValueError(
                "interface version mismatch: "
                f"{snapshot.get('interface_version')!r} != {INTERFACE_VERSION!r}")
        self.max_depth = int(snapshot["max_depth"])
        self._loops = {k: LoopAdmission(**v)
                       for k, v in snapshot["loops"].items()}
        self._loop_state = dict(snapshot["loop_state"])
        self._mcs = {}
        for raw in snapshot["active"]:
            self._mcs[raw["mc_id"]] = Microcontroller(**raw)
        self._ledger = [Microcontroller(**raw) for raw in snapshot["ledger"]]

    # ------------------------------------------------------------------
    # Inspection (for proofs and the owning loop controller only)
    # ------------------------------------------------------------------

    def retired_ledger(self) -> List[Microcontroller]:
        return list(self._ledger)

    def get(self, mc_id: str) -> Optional[Microcontroller]:
        return self._mcs.get(mc_id)
