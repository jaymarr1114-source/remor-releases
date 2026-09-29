"""ACC-CTRL-1 -- the Acceptance Controller as a runtime entity.

James's three-level hierarchy (2026-09-28, standing): REMOR -> Executive
Controller (selects which loop is active) -> six loop controllers (manage
convergence) -> microcontrollers inside each loop (operate graphs; may
spawn smaller ones, all local to the loop; on resolution the stack
unwinds). This module is the Acceptance loop controller. Its convergence
process: dissatisfaction -> expansion -> retry, with completed != accepted
always.

Ownership (deliberate, no conflict with the 2026-09-27 Run Controller
decision): the Run Controller owns acceptance-loop INVOCATION (when the
loop runs in the cadence; run_controller.invoke_acceptance is untouched).
This controller owns the convergence MECHANISM (how the loop converges):
the standing four-panel verification, the present -> verdict flow, and
the dissatisfaction -> expansion -> retry loop. It SUBORDINATES the
existing machinery -- the V10-P6 AcceptanceDriver and M6's AcceptanceLoop
(Q8's present() inlet) -- by wrapping and owning them, never rewriting
them.

The four verification microcontrollers (acceptance_panels.py) live inside
this loop only. They are spawned and retired through the MicrocontrollerHarness
below. Spawn/retire/budget semantics conform to the shared interface frozen
by RUN-MICRO-1 (Run chat) -- see RUN_MICRO_1_INTERFACE.

The executive's view stays O(1): loop_status() returns only
{"loop": "acceptance", "state": "active"} no matter how many
microcontrollers are live inside. Retirement unwinds the local stack back
to this controller, which resolves to the executive (resolve_to_executive;
the Executive Controller itself is unbuilt -- RUN-EXEC-1 is queued in the
Run chat behind RUN-MICRO-1 -- so the seam is exposed honestly, not
invented).
"""

from __future__ import annotations

import itertools
import time
from typing import Any, Dict, List, Optional

from swarm_engine.core.acceptance_panels import (
    PanelContext, PanelVerdict, VerificationPanel, build_panels)
from swarm_engine.core.microcontroller import (
    INTERFACE_VERSION, LOOP_ACCEPTANCE, MicrocontrollerSubstrate, Refusal)
from swarm_engine.services.acceptance import Attempt


# ---------------------------------------------------------------------------
# the RUN-MICRO-1 seam -- BOUND (ACC-BIND-1)
# ---------------------------------------------------------------------------

RUN_MICRO_1_INTERFACE = INTERFACE_VERSION  # "microcontroller-interface/v1"
# Bound 2026-09-28 (ACC-BIND-1): the harness below delegates spawn/retire/
# budget/depth/admission/charge to the frozen shared substrate
# (runtime/core/microcontroller/substrate.py, 8d4cb2d). The substrate module
# is FROZEN -- this controller calls it, never edits it. The panels'
# verification logic does not depend on the interface.


class PanelHandle:
    """A live microcontroller inside this loop.

    `id` is this controller's local label (acc-mc-N); `mc_id` is the
    authoritative lifecycle identity on the shared substrate. `parent_id`
    is the LOCAL parent handle id (the controller's own tree); the
    substrate tracks parentage by mc_id."""
    _ids = itertools.count(1)

    def __init__(self, panel: VerificationPanel, mc_id: str,
                 parent_id: Optional[str] = None,
                 depth: int = 0, budget_s: float = 60.0):
        self.id = f"acc-mc-{next(PanelHandle._ids)}"
        self.mc_id = mc_id
        self.panel = panel
        self.parent_id = parent_id
        self.depth = depth
        self.budget_s = budget_s
        self.spawned_at = time.time()
        self.retired_at: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "mc_id": self.mc_id,
                "panel": self.panel.kind,
                "parent_id": self.parent_id, "depth": self.depth,
                "budget_s": self.budget_s,
                "spawned_at": self.spawned_at,
                "retired_at": self.retired_at}


class SpawnOutcome:
    """What harness.spawn returned. Refusals are values, never
    exceptions -- the frozen interface's discipline."""
    def __init__(self, ok: bool,
                 handle: Optional[PanelHandle] = None,
                 refusal: Optional[Refusal] = None):
        self.ok = ok
        self.handle = handle
        self.refusal = refusal


class MicrocontrollerHarness:
    """Spawn/retire for this controller's microcontrollers, DELEGATED to
    the frozen shared substrate (ACC-BIND-1). Every lifecycle operation
    goes through MicrocontrollerSubstrate; this harness keeps only the
    controller's own bookkeeping (panel callbacks, local tree, refusal
    log). No parallel loop-local lifecycle semantics remain.

    Local recursion: a panel may spawn smaller ones (parent=handle); the
    substrate caps depth and unwinds children-first back to the
    controller. Budgets are enforced fail-closed via cooperative charge():
    a panel that exceeds its budget cannot pass.
    """

    # The acceptance loop's bounds, expressed THROUGH the substrate:
    #   depth 4  -> substrate max_depth=4 on the controller-owned instance
    #                (stricter than the interface default 8; the tighter
    #                bound governs here)
    #   16 live  -> the loop's admission pool max_concurrent=16
    #   60s each -> per-spawn budget_s=60.0, enforced by charge()
    #   pool     -> 16 * 60s = 960s loop budget
    MAX_DEPTH = 4
    MAX_LIVE = 16
    PANEL_BUDGET_S = 60.0
    LOOP_BUDGET_S = MAX_LIVE * PANEL_BUDGET_S  # 960.0

    def __init__(self,
                 substrate: Optional[MicrocontrollerSubstrate] = None):
        # Controller-owned instance until the Executive (RUN-EXEC-1)
        # provides a shared one. max_depth=4 here is the acceptance
        # loop's own tighter bound, expressed through the substrate's
        # own parameter -- not a parallel check.
        self.substrate = substrate or MicrocontrollerSubstrate(
            max_depth=self.MAX_DEPTH)
        self.substrate.register_loop(
            LOOP_ACCEPTANCE, budget_s=self.LOOP_BUDGET_S,
            max_concurrent=self.MAX_LIVE)
        self.default_panel_budget_s = self.PANEL_BUDGET_S
        self._live: Dict[str, PanelHandle] = {}
        self._children: Dict[str, List[str]] = {}
        self._refusals: List[Refusal] = []

    def spawn(self, panel: VerificationPanel,
              parent: Optional[PanelHandle] = None,
              budget_s: Optional[float] = None) -> SpawnOutcome:
        """Spawn through the shared substrate. Returns SpawnOutcome --
        a refusal is a value carrying the interface's reason code, never
        an exception."""
        result = self.substrate.spawn(
            LOOP_ACCEPTANCE, f"panel:{panel.kind}",
            parent_id=parent.mc_id if parent is not None else None,
            budget_s=(self.default_panel_budget_s
                      if budget_s is None else budget_s),
            max_children=8)  # interface default; the harness never
                             # capped per-parent children
        if not result.ok:
            assert result.refusal is not None
            self._refusals.append(result.refusal)
            return SpawnOutcome(ok=False, refusal=result.refusal)
        mc = result.mc
        assert mc is not None
        handle = PanelHandle(
            panel, mc_id=mc.mc_id,
            parent_id=parent.id if parent is not None else None,
            depth=mc.depth,
            budget_s=(self.default_panel_budget_s
                      if budget_s is None else budget_s))
        self._live[handle.id] = handle
        if parent is not None:
            self._children.setdefault(parent.id, []).append(handle.id)
        panel.on_spawn()
        return SpawnOutcome(ok=True, handle=handle)

    def charge(self, handle: PanelHandle,
               seconds: float) -> tuple:
        """Cooperative budget charge through the substrate. Returns
        (exhausted, state)."""
        return self.substrate.charge(handle.mc_id, seconds)

    def apply_budget_gate(self, handle: PanelHandle,
                          verdict: PanelVerdict) -> PanelVerdict:
        """Charge the panel's measured work against its substrate budget.
        On exhaustion the verdict is refused -- fail-closed, named.
        Must be called BEFORE retire (the substrate only charges active
        microcontrollers)."""
        exhausted, state = self.charge(
            handle, max(0.0, verdict.elapsed_s))
        if exhausted:
            verdict.budget_exceeded = True
            verdict.passed = False
            verdict.reason += (
                f" [panel budget exhausted via the shared substrate: "
                f"{verdict.elapsed_s:.1f}s > {handle.budget_s:.1f}s "
                f"(mc {handle.mc_id} is {state})]")
        return verdict

    def retire(self, handle: PanelHandle) -> Optional[Refusal]:
        """Retire through the substrate (which cascade-retires children),
        then walk the local tree for panel callbacks and bookkeeping.
        Idempotent: retiring an already-retired handle is a no-op.
        A substrate Refusal is returned, never swallowed."""
        if handle.id not in self._live:
            return None
        # Honest outcome: never claim "resolved" on an exhausted mc --
        # the substrate would (correctly) flag it as fabricated.
        rec = self.substrate.get(handle.mc_id)
        outcome = ("exhausted" if rec is not None
                   and rec.state == "exhausted" else "resolved")
        result = self.substrate.retire(
            handle.mc_id, outcome=outcome, loop=LOOP_ACCEPTANCE)
        self._retire_local_tree(handle)
        if isinstance(result, Refusal):
            self._refusals.append(result)
            return result
        return None

    def _retire_local_tree(self, handle: PanelHandle) -> None:
        """Panel callbacks + bookkeeping for the handle and its local
        subtree (the substrate already cascade-retired their records)."""
        for child_id in list(self._children.get(handle.id, [])):
            child = self._live.get(child_id)
            if child is not None:
                self._retire_local_tree(child)
        self._children.pop(handle.id, None)
        if handle.parent_id is not None:
            sibs = self._children.get(handle.parent_id, [])
            if handle.id in sibs:
                sibs.remove(handle.id)
        handle.panel.on_retire()
        handle.retired_at = time.time()
        self._live.pop(handle.id, None)

    def live(self) -> List[PanelHandle]:
        return list(self._live.values())

    def refusals(self) -> List[Refusal]:
        """Every spawn/retire refusal this harness has seen, in order."""
        return list(self._refusals)


# ---------------------------------------------------------------------------
# the controller
# ---------------------------------------------------------------------------

class AcceptanceController:
    """The Acceptance loop as a runtime entity. Owns the convergence
    mechanism; subordinates (wraps, never rewrites) the V10-P6 driver and
    M6's loop; operates the four standing verification microcontrollers."""

    def __init__(self, driver: Any = None,
                 repo_root: Optional[str] = None,
                 observer: Any = None):
        self.driver = driver
        self.repo_root = repo_root
        self.observer = observer
        self.harness = MicrocontrollerHarness()
        self.observations: List[Dict[str, Any]] = []

    @classmethod
    def from_engine(cls, engine: Any, epistemic: Any, store_path: str,
                    repo_root: Optional[str] = None,
                    observer: Any = None) -> "AcceptanceController":
        """Build the subordinated driver the same way the Run Controller
        does (its _acceptance_driver), but owned by this controller."""
        from swarm_engine.services.acceptance import (
            AcceptanceLoop, AcceptanceStore)
        from swarm_engine.services.acceptance_driver import AcceptanceDriver
        self = cls(driver=None, repo_root=repo_root, observer=observer)
        loop = AcceptanceLoop(AcceptanceStore(store_path), epistemic,
                              engine=engine)
        self.driver = AcceptanceDriver(loop, controller=self)
        return self

    # -- observations (the driver's _observe calls land here) -------------

    def _observe(self, kind: str, content: str,
                 raw: Optional[Dict[str, Any]] = None) -> str:
        rec = {"kind": kind, "content": content, "raw": raw or {},
               "at": time.time()}
        self.observations.append(rec)
        if self.observer is not None:
            try:
                return self.observer._observe(kind, content, raw)
            except Exception:
                pass
        return ""

    # -- the controller-owned entry point --------------------------------

    @staticmethod
    def _require(result: Dict[str, Any], key: str) -> Any:
        v = result.get(key)
        if v is None or v == "" or v == [] or v == {}:
            raise ValueError(
                f"present_completion: result is missing required field "
                f"{key!r}; a fabricated or incomplete result cannot enter "
                f"acceptance")
        return v

    def _build_attempt(self, result: Dict[str, Any]) -> Attempt:
        return Attempt(
            approach_signature=list(self._require(
                result, "approach_signature")),
            plan=dict(self._require(result, "plan")),
            args=dict(result.get("args") or {}),
            result_summary=result.get("result_summary"),
            exec_ok=bool(result.get("exec_ok")))

    def _run_panels(self, result: Dict[str, Any],
                    attempt: Attempt) -> List[PanelVerdict]:
        """Spawn the standing panel, run each microcontroller, retire.
        The same four panels gate every completion -- no mission-specific
        wiring exists between this entry point and a refusal."""
        if self.driver is None:
            raise ValueError("_run_panels: no driver attached")
        engine = self.driver.loop.engine
        if engine is None:
            raise ValueError("_run_panels: no engine injected")
        ctx = PanelContext(result=result, attempt=attempt, engine=engine,
                           repo_root=self.repo_root or "",
                           run_started_at=time.time())
        verdicts: List[PanelVerdict] = []
        for panel in build_panels():
            outcome = self.harness.spawn(panel)
            if not outcome.ok:
                # Fail-closed: a microcontroller that cannot be admitted
                # cannot verify. The refusal names the interface reason.
                assert outcome.refusal is not None
                verdicts.append(PanelVerdict(
                    panel=panel.kind, passed=False,
                    reason=f"{panel.kind}: spawn refused "
                           f"({outcome.refusal.reason}): "
                           f"{outcome.refusal.message}; a microcontroller "
                           f"that cannot be admitted cannot verify",
                    evidence={"refusal": outcome.refusal.as_dict()}))
                break
            handle = outcome.handle
            assert handle is not None
            t0 = time.time()
            try:
                v = panel.verify(ctx)
            except Exception as e:
                v = PanelVerdict(
                    panel=panel.kind, passed=False,
                    reason=f"{panel.kind}: panel raised "
                           f"{type(e).__name__}: {e}; a panel that "
                           f"cannot verify refuses",
                    evidence={"error": str(e)},
                    elapsed_s=time.time() - t0)
            # Cooperative budget gate BEFORE retire: the panel's measured
            # work is charged against its substrate budget; exhaustion
            # refuses the verdict (fail-closed, named).
            v = self.harness.apply_budget_gate(handle, v)
            self.harness.retire(handle)
            verdicts.append(v)
            if not v.passed:
                break  # fail fast; the refusal names the first failing panel
        return verdicts

    def present_completion(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """A produced result enters the acceptance pathway through the
        controller. The standing four-panel verification runs FIRST: any
        refusal stops the result here, named and recorded. Only a result
        that passes all four panels reaches the subordinated driver's
        present_result (which runs its own auth gate and presents as
        CANDIDATE -- completed, never accepted-by-default)."""
        attempt = self._build_attempt(result)
        verdicts = self._run_panels(result, attempt)
        panel_dicts = [v.as_dict() for v in verdicts]
        failed = next((v for v in verdicts if not v.passed), None)
        if failed is not None:
            self._observe(
                "acceptance_refused",
                f"AcceptanceController: result for run "
                f"{result.get('run_id')} REFUSED by the {failed.panel} "
                f"panel: {failed.reason}",
                {"kind": "acceptance_refused",
                 "run_id": result.get("run_id"),
                 "panel": failed.panel, "reason": failed.reason,
                 "panels": panel_dicts})
            return {"status": "refused", "panel": failed.panel,
                    "reason": failed.reason, "panels": panel_dicts}
        rec = self.driver.present_result(result)
        self._observe(
            "acceptance_presented",
            f"AcceptanceController: run {rec.run_id} passed all four "
            f"panels and is presented as candidate; completed != accepted.",
            {"kind": "acceptance_presented", "run_id": rec.run_id,
             "panels": panel_dicts})
        return {"status": "presented", "run_id": rec.run_id,
                "state": rec.state.value, "rounds": rec.rounds,
                "panels": panel_dicts}

    # -- the convergence loop, owned here, executed by the driver --------

    def submit_verdict(self, run_id: str, satisfied: bool,
                       feedback: str = "",
                       unmet_criteria: Optional[List[str]] = None,
                       retry_held_out: Optional[List[Dict[str, Any]]] = None
                       ) -> Dict[str, Any]:
        """A verdict arrives. satisfied=True closes the loop (ACCEPTED).
        Dissatisfaction routes through the subordinated driver: near-miss
        persisted -> pool expansion steered by the near-miss -> steered
        retry authenticated -> re-presented. The controller owns the loop;
        the driver executes it."""
        return self.driver.submit_verdict(
            run_id, satisfied, feedback, unmet_criteria, retry_held_out)

    def drive(self, result: Dict[str, Any],
              verdicts: Any) -> Dict[str, Any]:
        """Synchronous end-to-end through the controller: present (with
        panels) -> verdict -> (dissatisfaction -> expansion -> retry ->
        re-present)* -> terminal. NOTE: drive() presents through the
        driver's present_result directly (the bench path); production
        entries use present_completion for the panel gate."""
        return self.driver.drive(result, verdicts)

    def evidence_chain(self, run_id: str) -> Dict[str, Any]:
        return self.driver.evidence_chain(run_id)

    # -- executive seams ---------------------------------------------------

    def loop_status(self) -> Dict[str, str]:
        """The executive's O(1) view of this loop. It sees only that
        Acceptance is active -- never the internal microcontroller
        population."""
        return {"loop": "acceptance", "state": "active"}

    def internal_population(self) -> List[Dict[str, Any]]:
        """Loop-internal visibility (NOT the executive view): live
        microcontrollers, for this controller's own operation."""
        return [h.as_dict() for h in self.harness.live()]

    def substrate_loop_view(self) -> Dict[str, Any]:
        """Loop-internal visibility through the shared substrate: the
        LoopView aggregates (no microcontroller ids or purposes -- the
        interface enforces that by construction). NOT the executive
        view; the executive gets loop_status() only."""
        return self.harness.substrate.loop_view(
            LOOP_ACCEPTANCE).as_dict()

    def resolve_to_executive(self) -> Dict[str, Any]:
        """Where this controller resolves on loop completion. The
        Executive Controller itself is unbuilt (RUN-EXEC-1 is queued in
        the Run chat behind RUN-MICRO-1): the seam is exposed honestly,
        not invented."""
        return {"loop": "acceptance", "resolution": "pending_executive",
                "note": "Executive Controller unbuilt (RUN-EXEC-1 queued "
                        "in the Run chat behind RUN-MICRO-1); this seam "
                        "is where the controller resolves."}
