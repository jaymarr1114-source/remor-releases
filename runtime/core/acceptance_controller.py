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
from swarm_engine.services.acceptance import Attempt


# ---------------------------------------------------------------------------
# the RUN-MICRO-1 seam (NAMED GATE)
# ---------------------------------------------------------------------------

RUN_MICRO_1_INTERFACE = "PENDING"
# Named gate: RUN-MICRO-1 (Run chat) freezes the shared microcontroller
# spawn/retire/budget interface for all six tracks. This harness implements
# loop-local spawn/retire/depth/budget semantics behind that seam and binds
# to the frozen interface on landing. The panels' verification logic does
# not depend on the interface. NEVER invent the interface here.


class PanelHandle:
    """A live microcontroller inside this loop."""
    _ids = itertools.count(1)

    def __init__(self, panel: VerificationPanel,
                 parent_id: Optional[str] = None,
                 depth: int = 0, budget_s: float = 60.0):
        self.id = f"acc-mc-{next(PanelHandle._ids)}"
        self.panel = panel
        self.parent_id = parent_id
        self.depth = depth
        self.budget_s = budget_s
        self.spawned_at = time.time()
        self.retired_at: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "panel": self.panel.kind,
                "parent_id": self.parent_id, "depth": self.depth,
                "budget_s": self.budget_s,
                "spawned_at": self.spawned_at,
                "retired_at": self.retired_at}


class MicrocontrollerHarness:
    """Loop-local spawn/retire for this controller's microcontrollers.

    Local recursion: a panel may spawn smaller ones (parent=handle); depth
    is bounded and retirement unwinds children-first back to the
    controller. Budgets are enforced fail-closed: a panel that exceeds its
    budget cannot pass (its verdict is refused). Hard preemption
    mid-execution is a RUN-MICRO-1-interface concern, documented at the
    seam, not invented here.
    """

    MAX_DEPTH = 4     # loop-local recursion bound
    MAX_LIVE = 16     # loop-local population bound

    def __init__(self):
        self._live: Dict[str, PanelHandle] = {}
        self._children: Dict[str, List[str]] = {}

    def spawn(self, panel: VerificationPanel,
              parent: Optional[PanelHandle] = None,
              budget_s: float = 60.0) -> PanelHandle:
        depth = (parent.depth + 1) if parent else 0
        if depth > self.MAX_DEPTH:
            raise RuntimeError(
                f"microcontroller spawn refused: depth {depth} exceeds "
                f"loop-local bound {self.MAX_DEPTH} (runaway-spawn guard)")
        if len(self._live) >= self.MAX_LIVE:
            raise RuntimeError(
                f"microcontroller spawn refused: {len(self._live)} live "
                f"exceeds loop-local bound {self.MAX_LIVE}")
        handle = PanelHandle(panel,
                             parent_id=parent.id if parent else None,
                             depth=depth, budget_s=budget_s)
        self._live[handle.id] = handle
        if parent is not None:
            self._children.setdefault(parent.id, []).append(handle.id)
        panel.on_spawn()
        return handle

    def retire(self, handle: PanelHandle) -> None:
        """Retire children first (unwind), then the handle itself."""
        for child_id in list(self._children.get(handle.id, [])):
            child = self._live.get(child_id)
            if child is not None:
                self.retire(child)
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
            handle = self.harness.spawn(panel)
            try:
                v = panel.verify(ctx)
            except Exception as e:
                v = PanelVerdict(
                    panel=panel.kind, passed=False,
                    reason=f"{panel.kind}: panel raised "
                           f"{type(e).__name__}: {e}; a panel that "
                           f"cannot verify refuses",
                    evidence={"error": str(e)})
            finally:
                self.harness.retire(handle)
            if v.elapsed_s > handle.budget_s:
                v.budget_exceeded = True
                v.passed = False
                v.reason += (f" [panel budget exhausted: "
                             f"{v.elapsed_s:.1f}s > {handle.budget_s:.1f}s]")
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

    def resolve_to_executive(self) -> Dict[str, Any]:
        """Where this controller resolves on loop completion. The
        Executive Controller itself is unbuilt (RUN-EXEC-1 is queued in
        the Run chat behind RUN-MICRO-1): the seam is exposed honestly,
        not invented."""
        return {"loop": "acceptance", "resolution": "pending_executive",
                "note": "Executive Controller unbuilt (RUN-EXEC-1 queued "
                        "in the Run chat behind RUN-MICRO-1); this seam "
                        "is where the controller resolves."}
