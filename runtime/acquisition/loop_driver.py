"""Q1 — Continuous cognition driver: the acquisition loop as one process.

James's developmental vision (standing): the cognitive, acquisition, and
admission loops must work like a brain — continuous background cognition
(loops that never stop, not on-demand pipelines), unified memory across
perception/learning/action, explicit developmental staging. The components
exist separately (M1 ingest, M2 distill, M5 repair, Q2 unified memory);
this module is the driver that runs them as one continuous process.

One cycle:
  1. ingest scan — the production ingestion inlet wires Q2's attempt_z as the
     Z-check (the injected seam Q2 built; the lexical path is never used here)
  2. distill scan — newly recorded technique deltas are adapted M1→M2 and fed
     through the real M2 DistillationLoop, autonomously
  3. quarantine sweep — M5's diagnose→repair driver runs on the loop's cadence
  4. observe — distillation outcomes, sweep outcomes, and the cycle summary
     return to the epistemic store as observations, so the next cycle sees them

Ownership: Q1 owns this file. Everything it drives is called, never edited.
The engine is INJECTED (the driver never constructs a SwarmEngine against a
shared DB — the single-owner lock forbids a second live engine per process).

M7 coordination: _note_gap / _close_gap are the gap-touching call sites.
They currently persist honest epistemic observations; when M7's gap-registry
API is up, these two methods are the switchover points (this file must never
write runtime/acquisition/gaps.py itself).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

#: Observation sources this driver reads and writes.
SRC_DELTA = "technique_delta"          # written by M1/Q5 ingestion (frozen)
SRC_OUTCOME = "loop_driver:distillation_outcome"  # this driver's processed marker
SRC_CYCLE = "loop_driver:cycle"        # this driver's cycle summary
SRC_GAP = "loop_driver:gap"            # gap-touching call sites (M7 switchover)

#: Cooperative per-cycle budget (seconds). Checked between steps/deltas;
#: a step already running is never killed mid-flight, the budget only
#: prevents starting new work.
DEFAULT_CYCLE_BUDGET_S = 600.0


class CognitionLoop:
    """The continuous acquisition loop. Construct once with the live engine."""

    def __init__(self, engine: Any, *, evidence_store: Any = None,
                 cycle_budget_s: float = DEFAULT_CYCLE_BUDGET_S) -> None:
        self.engine = engine
        # EVIDENCE-WIRE-1: epistemic resolves lazily so the loop can be
        # constructed at boot with a lazy engine proxy without forcing
        # engine construction on the serving thread (thread-affinity).
        self._epistemic = None
        self.evidence_store = evidence_store
        self.cycle_budget_s = float(cycle_budget_s)
        self._distill_loop = None  # lazy: DistillationLoop(engine, epistemic)
        self._registry = None   # lazy: primitive registry for inventory

    @property
    def epistemic(self) -> Any:
        if self._epistemic is None:
            self._epistemic = self.engine.intellect.epistemic
        return self._epistemic

    @epistemic.setter
    def epistemic(self, value: Any) -> None:
        self._epistemic = value

    # ------------------------------------------------------------------
    # Production ingestion inlet: attempt_z IS the live Z-check here
    # ------------------------------------------------------------------
    def _z_check(self, objective: str, evidence: Any) -> Any:
        """The injected Z-check callable for the ingestion gate.

        Shape matches the seam Q2 built: (objective, evidence) -> result
        with .reached / .classification. attempt_z never raises; the gate
        fails closed to "gap" on any error (see ingest._objective_covered).
        """
        from swarm_engine.intellect.unified_memory import attempt_z
        return attempt_z(objective, evidence, epistemic=self.epistemic)

    def ingest_demo(self, demo: Any, inventory: Any = None) -> Any:
        """Ingest an external demonstration through the production gate.

        This is the production wiring of Q2's boundary: the Z-check is the
        real planner-attempt attempt_z, not the lexical heuristic and not
        the None default. Returns the real IngestResult.
        """
        from swarm_engine.acquisition.ingest import (
            ingest_external_demonstration)
        if inventory is None:
            inventory = self._build_inventory()
        return ingest_external_demonstration(
            demo, inventory, self.epistemic,
            evidence_store=self.evidence_store, z_check=self._z_check)

    def _primitive_registry(self) -> Any:
        if self._registry is None:
            from swarm_engine.primitives import build_registry
            self._registry = build_registry()
        return self._registry

    def _build_inventory(self) -> Any:
        """Snapshot Z from the live engine.

        Bridges engine.capabilities.list() to build_inventory's interface
        (list_capabilities(status_filter="all")). The store spells the live
        state "active"; the interface spells it "ACTIVE" — the case is
        normalized at this seam, nothing else is changed.
        """
        from swarm_engine.acquisition.ingest import build_inventory

        class _Adapter:
            def __init__(self, store: Any) -> None:
                self._store = store

            def list_capabilities(self, status_filter: str = "all") -> Any:
                recs = self._store.list(status=status_filter, limit=10000)
                out = []
                for r in recs:
                    d = r.as_dict() if hasattr(r, "as_dict") else dict(r)
                    if str(d.get("status", "")).lower() == "active":
                        d["status"] = "ACTIVE"
                    out.append(d)
                return {"capabilities": out}

        return build_inventory(_Adapter(self.engine.capabilities),
                               self._primitive_registry())

    # ------------------------------------------------------------------
    # Cycle
    # ------------------------------------------------------------------
    def cycle(self, *, time_budget_s: Optional[float] = None) -> Dict[str, Any]:
        """Run one full loop cycle. Never raises: every step is isolated.

        Returns a CycleReport dict: per-step results, errors, timing, and
        whether the budget stopped further work.
        """
        budget = (self.cycle_budget_s if time_budget_s is None
                  else float(time_budget_s))
        started = time.monotonic()
        report: Dict[str, Any] = {
            "started_at": time.time(),
            "budget_s": budget,
            "budget_exceeded": False,
            "distilled": [],
            "distill_errors": [],
            "sweep": None,
            "sweep_error": None,
            "errors": [],
        }

        def _over_budget() -> bool:
            return (time.monotonic() - started) >= budget

        # -- step 1: distill newly recorded deltas ---------------------
        try:
            new_deltas = self._scan_new_deltas()
        except Exception as exc:  # the scan itself must not kill the cycle
            new_deltas = []
            report["errors"].append(f"delta_scan: {type(exc).__name__}: {exc}")

        for payload in new_deltas:
            if _over_budget():
                report["budget_exceeded"] = True
                break
            try:
                outcome = self._distill_delta(payload)
                report["distilled"].append(outcome)
            except Exception as exc:  # one bad delta never aborts the cycle
                delta_id = (payload.get("delta") or {}).get("delta_id", "?")
                report["distill_errors"].append(
                    {"delta_id": delta_id,
                     "error": f"{type(exc).__name__}: {exc}"})

        # -- step 2: quarantine sweep (M5's driver, on the loop's cadence) --
        if not _over_budget():
            try:
                report["sweep"] = self._sweep_quarantine()
            except Exception as exc:
                report["sweep_error"] = f"{type(exc).__name__}: {exc}"
        else:
            report["budget_exceeded"] = True

        report["elapsed_s"] = time.monotonic() - started

        # -- step 3: observe — the cycle's work returns to the store ----
        try:
            self._observe_cycle(report)
        except Exception as exc:
            report["errors"].append(f"observe: {type(exc).__name__}: {exc}")
        return report

    # ------------------------------------------------------------------
    # Delta scan + M1→M2 adaptation + distillation
    # ------------------------------------------------------------------
    def _scan_new_deltas(self) -> List[Dict[str, Any]]:
        """Technique deltas recorded since the last processed marker.

        Processed markers are the driver's own outcome observations, so the
        scan is restart-safe: a delta is new iff no outcome observation
        carries its delta_id.
        """
        processed = set()
        deltas = []
        for obs in self.epistemic.all_observations():
            src = getattr(obs, "source", "")
            raw = getattr(obs, "raw", None) or {}
            if src == SRC_OUTCOME and raw.get("delta_id"):
                processed.add(raw["delta_id"])
            elif src == SRC_DELTA and isinstance(raw.get("delta"), dict):
                deltas.append(raw)
        return [p for p in deltas
                if p["delta"].get("delta_id") not in processed]

    def _distill_delta(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Adapt one M1-schema delta to M2's schema and distill it.

        Never raises on a failed distillation — the failure is named in the
        returned outcome and persisted as an observation (the honest path,
        mirroring DistillationLoop.distill's own contract).
        """
        from swarm_engine.acquisition.delta import (
            DeltaRecord, DeltaValidationError)
        m1 = payload["delta"]
        delta_id = m1.get("delta_id", "?")
        outcome: Dict[str, Any] = {"delta_id": delta_id, "success": False,
                                  "reason": "", "promoted_name": None}

        actions = self._demo_actions_for(delta_id)
        m2 = self._adapt_m1_to_m2(m1, actions)
        try:
            m2.validate()
        except DeltaValidationError as exc:
            outcome["reason"] = f"delta rejected: {exc}"
            self._record_outcome(outcome, m1)
            return outcome

        result = self._distiller().distill(m2)
        outcome["success"] = bool(result.success)
        outcome["reason"] = result.reason or ""
        outcome["promoted_name"] = result.promoted_name
        outcome["route"] = getattr(result, "route", "")
        self._record_outcome(outcome, m1, result=result)
        if result.success:
            self._record_experience(m1, m2, result)
        return outcome

    def _distiller(self) -> Any:
        if self._distill_loop is None:
            from swarm_engine.acquisition.distill import DistillationLoop
            self._distill_loop = DistillationLoop(self.engine, self.epistemic)
        return self._distill_loop

    def _demo_actions_for(self, delta_id: str) -> List[Any]:
        """Recover the demo's observed actions from the persisted Evidence."""
        actions: List[Any] = []
        try:
            for ev in self.epistemic.evidence_for(delta_id):
                content = getattr(ev, "content", None) or {}
                for a in content.get("actions", []) or []:
                    if isinstance(a, dict):
                        actions.append(SimpleNamespace(**a))
                    else:
                        actions.append(a)
        except Exception:
            pass
        return actions

    @staticmethod
    def _adapt_m1_to_m2(m1: Dict[str, Any],
                       actions: List[Any]) -> Any:
        """Adapt the M1 Y-Z-T-E-D-V-C record to M2's distillation schema.

        DELTA-NAME-1 (2026-10-01): there is now exactly one DeltaRecord
        class in the tree — M2's (swarm_engine.acquisition.delta), with
        validate() and the causal discipline. The M1 side is a frozen
        plain-dict ingestion schema (built by ingest._m1_delta_dict),
        not a class; this adapter remains the documented seam between
        the two — field-by-field, nothing guessed: I/O evidence comes
        from the observed demo actions via Q2's evidence_from_demo_actions
        convention.
        """
        from swarm_engine.acquisition.delta import DeltaRecord
        from swarm_engine.intellect.unified_memory import (
            evidence_from_demo_actions)
        y = m1.get("Y") or {}
        z = m1.get("Z") or {}
        t = m1.get("T") or {}
        pairs = evidence_from_demo_actions(actions)
        evidence = [{"input": dict(i), "output": o} for i, o in pairs]
        action_names = [getattr(a, "name", "?") for a in actions]
        return DeltaRecord(
            objective=str(m1.get("X") or ""),
            external_actions=(
                f"external agent ({y.get('source', '?')}) performed "
                f"{y.get('action_count', len(actions))} observed actions: "
                + ", ".join(action_names[:12])),
            prior_capability=(
                "inventory at ingestion: "
                f"{len(z.get('capabilities', []))} capabilities, "
                f"{len(z.get('primitives', []))} primitives, "
                f"vocabulary_size={z.get('vocabulary_size', '?')}"),
            capability_gap=str(m1.get("gap") or ""),
            technique=str(t.get("technique_name") or ""),
            evidence=evidence,
            dependencies=list(m1.get("D") or []),
            verification=dict(m1.get("V") or {}),
            delta_id=str(m1.get("delta_id") or ""),
            source="loop_driver",
        )

    def _record_outcome(self, outcome: Dict[str, Any], m1: Dict[str, Any],
                        result: Any = None) -> None:
        """Persist the distillation outcome: the processed marker AND the
        observe step for this delta. The next scan sees the marker.
        Written through the unified-memory facade."""
        from swarm_engine.intellect.unified_memory import record_experience
        record_experience(
            self.epistemic,
            origin_loop="acquisition",
            kind="distillation_outcome",
            content=(f"loop_driver distillation outcome: "
                     f"{outcome['delta_id']} success={outcome['success']} "
                     f"reason={outcome['reason'][:200]}"),
            raw={"delta_id": outcome["delta_id"],
                 "success": outcome["success"],
                 "reason": outcome["reason"],
                 "promoted_name": outcome["promoted_name"],
                 "route": outcome.get("route", ""),
                 "objective": m1.get("X"),
                 "verdict_admitted": bool(getattr(result, "verdict_admitted",
                                                  False)) if result else False,
                 "heldout": (f"{getattr(result, 'heldout_passed', '?')}/"
                             f"{getattr(result, 'heldout_examples', '?')}")
                            if result else "?"},
            causal_chain=[outcome["delta_id"]],
            source=SRC_OUTCOME,
        )

    def _record_experience(self, m1: Dict[str, Any], m2: Any,
                           result: Any) -> None:
        """On success, write the M2-style experience record (Q2's schema)
        so later Z-checks see prior experience on neighboring objectives."""
        from swarm_engine.intellect.unified_memory import (
            record_distillation_experience)
        record_distillation_experience(
            self.epistemic,
            objective=m2.objective,
            delta_id=m2.delta_id,
            Y=m2.external_actions,
            Z=m2.prior_capability,
            T=m2.technique,
            E=[{"input": i, "output": o}
               for i, o in m2.evidence_examples()],
            D=m2.dependencies,
            V={"distillation_route": getattr(result, "route", ""),
               "verdict_admitted": bool(getattr(result, "verdict_admitted",
                                                False))},
            C={"promoted_name": result.promoted_name},
        )

    # ------------------------------------------------------------------
    # Quarantine sweep (M5's driver, on the loop's cadence)
    # ------------------------------------------------------------------
    def _sweep_quarantine(self) -> Dict[str, Any]:
        """Run M5's diagnose→repair sweep. Defensive: a sweep failure is
        recorded, never raised — the loop must survive its own repair step.

        Q4 is hardening this driver for unattended execution; this call
        site stays a clean single call so Q4's hardened version is a
        drop-in replacement.
        """
        from swarm_engine.synthesis.integrity import repair_all_quarantined
        try:
            result = repair_all_quarantined(
                self.engine, caller="cognition_loop",
                reason="loop_driver cadence sweep")
        except Exception as exc:
            return {"count": 0, "results": {},
                    "error": f"{type(exc).__name__}: {exc}"}
        actions = sum(
            1 for r in (result.get("results") or {}).values()
            if isinstance(r, dict) and r.get("action") not in
            ("noop", "no-op", None))
        if actions or result.get("count"):
            from swarm_engine.intellect.unified_memory import record_experience
            record_experience(
                self.epistemic,
                origin_loop="acquisition",
                kind="quarantine_sweep",
                content=(f"loop_driver quarantine sweep: "
                         f"{result.get('count', 0)} quarantined seen, "
                         f"{actions} actions taken"),
                raw={"kind": "quarantine_sweep",
                     "count": result.get("count", 0),
                     "actions": actions,
                     "results": result.get("results", {})},
                source=SRC_CYCLE,
            )
        return result

    # ------------------------------------------------------------------
    # Observe: the cycle's work returns to the store
    # ------------------------------------------------------------------
    def _observe_cycle(self, report: Dict[str, Any]) -> None:
        """Write the cycle summary iff the cycle did anything.

        A cycle with no new deltas and no sweep actions is a true no-op:
        nothing is written. Steady-state cycles are silent, which is what
        "safe to run on a schedule" requires.
        """
        did_work = (bool(report["distilled"]) or bool(report["distill_errors"])
                    or (report["sweep"] is not None
                        and report["sweep"].get("count"))
                    or report["sweep_error"] or bool(report["errors"]))
        if not did_work:
            return
        from swarm_engine.intellect.unified_memory import record_experience
        record_experience(
            self.epistemic,
            origin_loop="acquisition",
            kind="cycle_summary",
            content=(f"loop_driver cycle: {len(report['distilled'])} distilled, "
                     f"{len(report['distill_errors'])} distill errors, "
                     f"sweep_count={(report['sweep'] or {}).get('count')}, "
                     f"budget_exceeded={report['budget_exceeded']}"),
            raw={"kind": "cycle_summary",
                 "distilled": report["distilled"],
                 "distill_errors": report["distill_errors"],
                 "sweep": report["sweep"],
                 "sweep_error": report["sweep_error"],
                 "errors": report["errors"],
                 "budget_exceeded": report["budget_exceeded"],
                 "elapsed_s": round(report["elapsed_s"], 2)},
            source=SRC_CYCLE,
        )

    # ------------------------------------------------------------------
    # Gap-touching call sites — M7 switchover points
    # ------------------------------------------------------------------
    def _note_gap(self, kind: str, detail: Dict[str, Any]) -> str:
        """Record an open gap. Written through the unified-memory facade
        (as an experience record); when M7's gap-registry API is up, this
        method registers through it instead. This method is the switchover
        point — callers do not change."""
        import uuid
        from swarm_engine.intellect.unified_memory import record_experience
        gap_id = f"gap_{uuid.uuid4().hex[:12]}"
        record_experience(
            self.epistemic,
            origin_loop="acquisition",
            kind="gap_open",
            content=f"loop_driver gap [{kind}]: {gap_id}",
            raw={"gap_id": gap_id, "kind": kind, "open": True,
                 "detail": detail},
            source=SRC_GAP,
        )
        return gap_id

    def _close_gap(self, gap_id: str, evidence: Dict[str, Any]) -> None:
        """Close a gap previously opened by _note_gap. Same switchover.

        Written through the unified-memory facade."""
        from swarm_engine.intellect.unified_memory import record_experience
        record_experience(
            self.epistemic,
            origin_loop="acquisition",
            kind="gap_close",
            content=f"loop_driver gap closed: {gap_id}",
            raw={"gap_id": gap_id, "open": False,
                 "closing_evidence": evidence},
            source=SRC_GAP,
        )
