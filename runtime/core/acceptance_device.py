"""Device-path acceptance inlet (ACC-P6-1).

The device dispatch path -- POST /api/intent/dispatch -> metering ->
scheduler run -> NLToolDispatcher.dispatch()/dispatch_by_id() ->
_dispatch_validated() -> _execute_and_record() -> real Composer execution
-> intent_dispatches row (ok=1) -> DispatchResult -- completes runs
WITHOUT ever entering the Acceptance Controller: run_controller's
invoke_acceptance has no production callers, and nothing on the dispatch
path presents, panels, or verdicts. A device run is "done" the moment the
HTTP 200 goes out. That is the bypass this module closes.

What this module does:
  * adapts device dispatch evidence into the Acceptance Controller's OWN
    entry points (present_completion / submit_verdict). One acceptance
    path for bench and device -- no parallel driver, no parallel panels,
    no parallel store, no forked acceptance logic.
  * derives held-out expectations honestly: explicit caller-supplied
    held-outs first, then the gap registry's closed-gap verified examples
    for the dispatched capability, else none -- in which case the
    correctness panel fail-closes with the true reason named (a result
    with nothing to execute against cannot pass).
  * reconciles intent_dispatches against the acceptance store: a
    completion recorded without ever entering acceptance is NAMED
    (acceptance_bypass_detected), never silently accepted and never
    given a fabricated acceptance record.

What this module does NOT do:
  * it never breaks the dispatch: every entry point used from the
    dispatch path is guarded so an acceptance failure degrades to an
    announced note, never an exception in the dispatch.
  * it never invents ground truth: held-outs are executed, not trusted;
    a supplied held-out that is wrong fails correctness honestly.
  * hardware acceptance stays James's gate: bench results here are
    mechanism evidence, classified as such.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Dict, List, Optional


def _validate_held_outs(specs: Any) -> List[Dict[str, Any]]:
    """Shape-check caller-supplied held-outs. Wrong shapes are dropped
    loudly (named in the returned note list is the caller's job); only
    well-formed {args: dict, expected: ...} specs survive. A wrong
    EXPECTED value is not detectable here -- it is executed by the
    panels, which is exactly the honesty property wanted."""
    out: List[Dict[str, Any]] = []
    if not isinstance(specs, (list, tuple)):
        return out
    for s in specs:
        if (isinstance(s, dict) and isinstance(s.get("args"), dict)
                and "expected" in s):
            out.append({"args": dict(s["args"]), "expected": s["expected"]})
    return out


class DeviceAcceptanceAdapter:
    """The device dispatch path's inlet into the Acceptance Controller.

    Built once per engine (cached by the dispatch hook). Owns one
    AcceptanceController built exactly the way the Run Controller builds
    its driver (from_engine over the same engine + epistemic store +
    the shared acceptance.db), so bench and device share one acceptance
    record store.
    """

    def __init__(self, controller: Any, engine: Any,
                 store_path: str) -> None:
        self.controller = controller
        self._engine = engine
        self._store_path = store_path
        self.observations: List[Dict[str, Any]] = []

    # -- presentation-attempt ledger ---------------------------------------
    # A refused presentation creates no acceptance record (panels gate
    # before the driver), so the acceptance store alone cannot tell
    # "entered and refused" from "never entered". This ledger records
    # every inlet attempt; reconcile() treats "no attempt AND no
    # record" as the bypass. Same acceptance.db, no new store file.

    def _note_attempt(self, dispatch_id: str, outcome: str) -> None:
        try:
            con = sqlite3.connect(self._store_path)
            try:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS "
                    "device_presentation_attempts "
                    "(dispatch_id TEXT PRIMARY KEY, at REAL, outcome TEXT)")
                con.execute(
                    "INSERT OR REPLACE INTO device_presentation_attempts "
                    "(dispatch_id, at, outcome) VALUES (?,?,?)",
                    (dispatch_id, time.time(), outcome))
                con.commit()
            finally:
                con.close()
        except Exception:
            pass

    def _attempted_ids(self) -> set:
        try:
            con = sqlite3.connect(self._store_path)
            try:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS "
                    "device_presentation_attempts "
                    "(dispatch_id TEXT PRIMARY KEY, at REAL, outcome TEXT)")
                rows = con.execute(
                    "SELECT dispatch_id FROM device_presentation_attempts"
                ).fetchall()
            finally:
                con.close()
            return {r[0] for r in rows}
        except Exception:
            return set()

    # -- construction ----------------------------------------------------

    @classmethod
    def from_engine(cls, engine: Any,
                    repo_root: Optional[str] = None,
                    store_path: Optional[str] = None
                    ) -> "DeviceAcceptanceAdapter":
        """Build over a live SwarmEngine. The epistemic store is M6's
        frozen record API, called through -- never reimplemented."""
        from swarm_engine.core.acceptance_controller import (
            AcceptanceController)
        intellect = getattr(engine, "intellect", None)
        epistemic = getattr(intellect, "epistemic", None)
        if epistemic is None:
            raise ValueError(
                "DeviceAcceptanceAdapter: engine has no intellect.epistemic; "
                "the acceptance loop cannot record without M6's epistemic "
                "store")
        db_path = getattr(engine, "db_path", None) or "swarm_engine.db"
        if store_path is None:
            # The same file the Run Controller's _acceptance_driver uses:
            # one acceptance store for every inlet.
            store_path = os.path.join(
                os.path.dirname(os.path.abspath(db_path)),
                "acceptance.db")
        if repo_root is None:
            repo_root = os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))
        controller = AcceptanceController.from_engine(
            engine, epistemic, store_path, repo_root=repo_root)
        return cls(controller, engine, store_path)

    # -- held-out derivation ----------------------------------------------

    def derive_held_outs(self, capability_id: str,
                         explicit: Any = None) -> List[Dict[str, Any]]:
        """Held-out precedence: explicit caller-supplied specs, then the
        gap registry's closed-gap verified examples for this capability,
        then none (the correctness panel fail-closes, named)."""
        if explicit is not None:
            return _validate_held_outs(explicit)
        try:
            from swarm_engine.acquisition.gaps import (
                GapRegistry, STATUS_CLOSED)
            from swarm_engine.synthesis.compose_inlet import (
                _gap_examples)
            reg = GapRegistry(self._engine)
            out: List[Dict[str, Any]] = []
            for gap in reg.list_gaps(status=STATUS_CLOSED):
                ce = getattr(gap, "closing_evidence", None) or {}
                if (isinstance(ce, dict)
                        and ce.get("capability_id") == capability_id):
                    _, held = _gap_examples(gap)
                    for args, expected in held:
                        if isinstance(args, dict):
                            out.append({"args": dict(args),
                                        "expected": expected})
            return out
        except Exception:
            return []

    # -- the device inlet --------------------------------------------------

    def present_device_dispatch(
            self, dispatch_id: str, request_text: str, rec: Any,
            args: Dict[str, Any], value: Any, route_via: Optional[str],
            held_out: Any = None,
            file_artifacts: Optional[List[str]] = None) -> Dict[str, Any]:
        """A successful device dispatch enters acceptance. Returns the
        controller's outcome dict (status presented | refused, panels,
        run_id, state). Raises only for genuinely missing evidence --
        the dispatch-path hook catches everything and degrades to a
        note, so the dispatch itself never breaks."""
        if not dispatch_id:
            raise ValueError(
                "present_device_dispatch: no dispatch_id; a result with "
                "no dispatch identity cannot enter acceptance")
        if rec is None or not isinstance(getattr(rec, "plan", None), dict):
            raise ValueError(
                "present_device_dispatch: no capability plan; a result "
                "with no plan cannot enter acceptance")
        result = {
            "run_id": dispatch_id,
            "goal": request_text or "",
            "approach_signature": list(getattr(rec, "ops", None) or []),
            "plan": dict(rec.plan),
            "args": dict(args or {}),
            "result_summary": value,
            "exec_ok": True,
            "held_out": self.derive_held_outs(
                getattr(rec, "capability_id", ""), explicit=held_out),
            "route_via": route_via or "device_dispatch",
            "device_dispatch_id": dispatch_id,
        }
        if file_artifacts:
            result["file_artifacts"] = list(file_artifacts)
        self._note_attempt(dispatch_id, "entered")
        try:
            outcome = self.controller.present_completion(result)
        except Exception:
            self._note_attempt(dispatch_id, "inlet_failed")
            raise
        self._note_attempt(dispatch_id,
                           "presented"
                           if outcome.get("status") == "presented"
                           else "refused")
        return outcome

    # -- device verdicts -----------------------------------------------------

    def submit_device_verdict(
            self, dispatch_id: str, capability_id: str, satisfied: bool,
            feedback: str = "",
            unmet_criteria: Optional[List[str]] = None,
            held_out: Any = None) -> Dict[str, Any]:
        """Device dissatisfaction routes through the controller's
        submit_verdict: the EXISTING near-miss -> expansion -> steered
        retry -> re-presentation loop. retry_held_out uses the same
        derivation precedence as presentation."""
        retry_held_out = self.derive_held_outs(
            capability_id, explicit=held_out)
        try:
            return self.controller.submit_verdict(
                dispatch_id, satisfied, feedback=feedback,
                unmet_criteria=unmet_criteria,
                retry_held_out=retry_held_out or None)
        except ValueError as exc:
            # The driver's present_retry fail-closes by raising when the
            # steered attempt cannot be authenticated (e.g. derived
            # held-outs belong to the original approach, not the steered
            # one). The verdict and near-miss are already recorded; the
            # refusal is translated to a named status here so a device
            # verdict submission never raises out of the inlet. The
            # check itself is not weakened: the retry stays refused.
            return {"status": "retry_refused", "run_id": dispatch_id,
                    "reason": f"{type(exc).__name__}: {exc}"}

    # -- bypass reconciliation -----------------------------------------------

    def reconcile(self, limit: int = 200) -> Dict[str, Any]:
        """Find completions recorded without ever entering acceptance.

        Scans intent_dispatches (ok=1) for dispatch_ids with neither an
        acceptance record NOR a presentation attempt in the ledger. Each
        is NAMED via an acceptance_bypass_detected observation --
        reopened, never silently accepted, and never given a fabricated
        acceptance record (only digests survive in the dispatch table;
        there is no plan to re-verify, so presentation is impossible and
        is not faked). A run that entered and was refused IS in the
        ledger and is not flagged: refusal is a verdict, not a bypass."""
        db_path = getattr(self._engine, "db_path", None)
        if not db_path or not os.path.exists(db_path):
            return {"checked": 0, "bypassed": [],
                    "note": "engine db unavailable; nothing reconciled"}
        try:
            con = sqlite3.connect(db_path)
            try:
                rows = con.execute(
                    "SELECT dispatch_id, ts, capability_id, request_text "
                    "FROM intent_dispatches WHERE ok=1 "
                    "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
            finally:
                con.close()
        except Exception as exc:
            return {"checked": 0, "bypassed": [],
                    "note": f"intent_dispatches unreadable: {exc!r}"}
        from swarm_engine.services.acceptance import AcceptanceStore
        store = AcceptanceStore(self._store_path)
        attempted = self._attempted_ids()
        bypassed: List[Dict[str, Any]] = []
        for dispatch_id, ts, capability_id, request_text in rows:
            try:
                seen = (dispatch_id in attempted
                        or store.get(dispatch_id) is not None)
            except Exception:
                seen = dispatch_id in attempted
            if not seen:
                entry = {"dispatch_id": dispatch_id, "ts": ts,
                         "capability_id": capability_id,
                         "request_text": (request_text or "")[:200]}
                bypassed.append(entry)
                note = (f"DeviceAcceptanceAdapter: completion "
                        f"{dispatch_id} (capability "
                        f"{str(capability_id)[:12]}...) recorded in "
                        f"intent_dispatches with ok=1 but never entered "
                        f"acceptance: no acceptance record exists. "
                        f"Reopened, not accepted.")
                self.observations.append(
                    {"kind": "acceptance_bypass_detected",
                     "content": note, "raw": entry, "at": time.time()})
                try:
                    self.controller._observe(
                        "acceptance_bypass_detected", note,
                        {"kind": "acceptance_bypass_detected", **entry})
                except Exception:
                    pass
        return {"checked": len(rows), "bypassed": bypassed}
