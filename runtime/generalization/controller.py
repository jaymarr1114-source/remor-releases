"""GEN-CTRL-1: the Generalization Controller as a runtime entity.

Owns the loop convergence process for the generalization loop:

    probe a novel task -> verify -> expand the envelope or mark the bound

This file is orchestration only. It subordinates (wrap and own, NEVER
rewrite):
- ``runtime/acquisition/generalize_driver.generalize_for_task`` (V10-P5)
  -- bound-constant re-parameterization leg;
- ``runtime/synthesis`` ``PlanComposer`` + ``q8_authenticate`` (V10-COMPOSE
  + INLET) -- planner-level composition leg;
- ``DistillationLoop.generalize`` (M2/Q6) -- via the driver;
- ``admit_as_engine`` + the Q8 auth inlet (V10-P6) -- the only admission
  path; the controller never weakens it;
- ``record_experience`` / ``read_experiences`` (V10-P1) -- envelope
  records; refusals and bounds are first-class records.

Microcontroller discipline (RUN-MICRO-1 frozen interface
``microcontroller-interface/v1``): one loop-rooted microcontroller per
cycle, one child per probe leg. Depth/budget caps are honored; on
resolution the local stack unwinds bottom-up to the controller. The
executive sees only ``LoopView`` -- "Generalization is active" while
the loop works, never microcontroller ids or purposes.

A cycle that crosses records an envelope EXPANSION; a cycle whose
mechanisms fail honestly records a MARKED BOUND (never an admission).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.core.microcontroller import (
    INTERFACE_VERSION,
    LOOP_GENERALIZATION,
    MC_EXHAUSTED,
    MC_RESOLVED,
    LoopView,
    MicrocontrollerSubstrate,
)

EXPECTED_INTERFACE = "microcontroller-interface/v1"

ENVELOPE_KIND = "generalization_envelope"
ENVELOPE_BASELINE_KIND = "generalization_envelope_baseline"
ORIGIN_LOOP = "generalization"

# GEN-STRUCT-1's published envelope (the mechanism-level baseline the
# controller seeds and then extends per technique).
BASELINE_HOLDS = [
    "unary pipelines over distilled techniques (e.g. negate(square(x)))",
    "map/filter over distilled techniques",
    "second-order reuse of one distilled technique (e.g. square(square(x)))",
    "bound-constant re-parameterization (DistillationLoop.generalize)",
]
BASELINE_BREAKS = [
    "binary combinations of distilled techniques (e.g. divide(5, square(x))): "
    "composer head_shape_ok admits only 1-input or (coll,fn) heads",
    "operator insertion into a distilled program: DistillationLoop.generalize "
    "is bound-constant substitution only",
    "new-branch synthesis: the composer never searches control steps",
]


@dataclass
class CycleResult:
    """What one probe -> verify -> expand/mark cycle produced."""

    outcome: str  # "crossed" | "bound_marked" | "budget_exhausted" | "error"
    mechanism: str  # "generalize" | "compose" | "none"
    capability_id: Optional[str] = None
    promoted_name: Optional[str] = None
    heldout: str = ""
    reason: str = ""
    envelope_observation_id: Optional[str] = None
    root_mc_id: str = ""
    leg_mc_ids: List[str] = field(default_factory=list)
    executive_views_during_cycle: List[str] = field(default_factory=list)
    retired_clean: bool = False


class GeneralizationController:
    """Runtime entity owning the generalization loop's convergence."""

    def __init__(self, engine: Any, *, epistemic: Any = None,
                 substrate: Optional[MicrocontrollerSubstrate] = None,
                 loop_budget_s: float = 7200.0,
                 leg_budget_s: float = 1200.0,
                 max_concurrent: int = 8) -> None:
        if INTERFACE_VERSION != EXPECTED_INTERFACE:
            raise RuntimeError(
                f"generalization controller requires {EXPECTED_INTERFACE}, "
                f"substrate reports {INTERFACE_VERSION!r}")
        self.engine = engine
        self.epistemic = epistemic if epistemic is not None \
            else self._resolve_epistemic(engine)
        if self.epistemic is None:
            raise RuntimeError(
                "GeneralizationController: no epistemic handle available; "
                "envelope records are load-bearing provenance, refusing to "
                "run without them")
        self.substrate = substrate or MicrocontrollerSubstrate()
        self.leg_budget_s = float(leg_budget_s)
        self.substrate.register_loop(
            LOOP_GENERALIZATION,
            budget_s=float(loop_budget_s),
            max_concurrent=int(max_concurrent))

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _resolve_epistemic(engine: Any) -> Any:
        intellect = getattr(engine, "intellect", None)
        return getattr(intellect, "epistemic", None)

    # ------------------------------------------------------------------ status

    def loop_view(self) -> LoopView:
        """The ONLY type the executive layer may consume."""
        return self.substrate.loop_view(LOOP_GENERALIZATION)

    def status(self) -> str:
        """Executive-facing loop state: 'Generalization is active|idle'."""
        view = self.loop_view()
        return (f"Generalization is {view.state} "
                f"(active={view.active_count} spawned={view.total_spawned} "
                f"retired={view.total_retired} refused={view.total_refused})")

    # ------------------------------------------------------------------ envelope

    def seed_baseline_envelope(self) -> str:
        """Write GEN-STRUCT-1's published envelope as the baseline record."""
        from swarm_engine.intellect.unified_memory import record_experience
        return record_experience(
            self.epistemic,
            origin_loop=ORIGIN_LOOP,
            kind=ENVELOPE_BASELINE_KIND,
            content="generalization envelope baseline (GEN-STRUCT-1): "
                    "unary pipelines + map/filter over distilled techniques "
                    "hold; binary combinations, operator insertion, and new "
                    "branches are marked bounds",
            raw={
                "holds": list(BASELINE_HOLDS),
                "breaks": list(BASELINE_BREAKS),
                "seeded_from": "GEN-STRUCT-1_REPORT.md",
            },
            source="generalization/controller",
        )

    def envelope_for(self, promoted_name: str,
                     limit: int = 100) -> Dict[str, Any]:
        """Baseline + per-technique envelope records for one technique."""
        from swarm_engine.intellect.unified_memory import read_experiences
        baseline = read_experiences(
            self.epistemic, kind=ENVELOPE_BASELINE_KIND, limit=5)
        mine = [
            r for r in read_experiences(
                self.epistemic, kind=ENVELOPE_KIND, limit=limit)
            if (r.get("raw") or {}).get("technique_promoted_name")
            == promoted_name
        ]
        return {"baseline": baseline, "technique_records": mine}

    def _record_envelope(self, *, technique_ref: Dict[str, Any],
                         novel_goal: str, mechanism: str, outcome: str,
                         heldout: str, capability_id: str,
                         composed_of: List[str], reason: str,
                         root_mc_id: str, leg_mc_ids: List[str],
                         holds: List[str], breaks: List[str]) -> str:
        from swarm_engine.intellect.unified_memory import record_experience
        content = (f"generalization envelope: "
                   f"{technique_ref.get('promoted_name')} x {novel_goal[:60]} "
                   f"-> {outcome} via {mechanism}")
        return record_experience(
            self.epistemic,
            origin_loop=ORIGIN_LOOP,
            kind=ENVELOPE_KIND,
            content=content,
            raw={
                "technique_promoted_name": technique_ref.get("promoted_name"),
                "technique_capability_id": technique_ref.get("capability_id"),
                "novel_goal": novel_goal,
                "mechanism": mechanism,
                "outcome": outcome,
                "heldout": heldout,
                "capability_id": capability_id,
                "composed_of": list(composed_of),
                "reason": reason,
                "root_mc_id": root_mc_id,
                "leg_mc_ids": list(leg_mc_ids),
                "holds": list(holds),
                "breaks": list(breaks),
            },
            source="generalization/controller",
        )

    # ------------------------------------------------------------------ cycle

    def run_cycle(self, *, technique_ref: Dict[str, Any], novel_goal: str,
                  train_examples: List[Any], held_out: List[Any],
                  params: Dict[str, Any], output_kind: Any,
                  gap_id: Optional[str] = None,
                  admission_name: Optional[str] = None) -> CycleResult:
        """One probe -> verify -> expand/mark convergence cycle.

        Leg 1 (generalize): bound-constant re-parameterization through the
        subordinated generalize_driver. Leg 2 (compose): planner-level
        composition over distilled primitives through the subordinated
        PlanComposer + Q8 inlet, admitted only via admit_as_engine.
        Each leg runs in its own child microcontroller; budgets are
        charged cooperatively and honored honestly.
        """
        gap = gap_id or f"gen-ctrl-1:{novel_goal[:40]}"
        views: List[str] = []
        leg_ids: List[str] = []

        root = self.substrate.spawn(
            LOOP_GENERALIZATION,
            purpose=f"generalization-cycle:{novel_goal[:48]}",
            budget_s=2 * self.leg_budget_s + 60.0)
        if not root.ok:
            return CycleResult(
                outcome="error", mechanism="none", root_mc_id="",
                reason=f"root spawn refused: {root.refusal.reason}")
        root_id = root.mc.mc_id
        views.append(self.loop_view().state)

        def _finish(outcome: str, mechanism: str, **kw: Any) -> CycleResult:
            # retire() returns a Microcontroller on success, a Refusal
            # (which carries .reason) on failure.
            res = self.substrate.retire(root_id, MC_RESOLVED,
                                        loop=LOOP_GENERALIZATION)
            retired_clean = not hasattr(res, "reason")
            views.append(self.loop_view().state)
            return CycleResult(
                outcome=outcome, mechanism=mechanism, root_mc_id=root_id,
                leg_mc_ids=list(leg_ids),
                executive_views_during_cycle=list(views),
                retired_clean=retired_clean, **kw)

        def _run_leg(purpose: str, fn: Any) -> tuple:
            """Spawn a child mc, run fn, charge, retire honestly."""
            leg = self.substrate.spawn(
                LOOP_GENERALIZATION, purpose=purpose, parent_id=root_id,
                budget_s=self.leg_budget_s)
            if not leg.ok:
                return None, f"leg spawn refused: {leg.refusal.reason}"
            leg_id = leg.mc.mc_id
            leg_ids.append(leg_id)
            views.append(self.loop_view().state)
            t0 = time.monotonic()
            try:
                out = fn()
            except Exception as exc:  # noqa: BLE001 -- honest error path
                out = exc
            elapsed = time.monotonic() - t0
            exhausted, _state = self.substrate.charge(leg_id, elapsed)
            if exhausted:
                # Honest: do NOT claim resolved on an exhausted mc (the
                # substrate would flag fabricated_resolve_on_exhausted).
                self.substrate.retire(leg_id, MC_EXHAUSTED,
                                      loop=LOOP_GENERALIZATION)
                views.append(self.loop_view().state)
                return None, "budget_exhausted"
            self.substrate.retire(leg_id, MC_RESOLVED,
                                  loop=LOOP_GENERALIZATION)
            views.append(self.loop_view().state)
            return out, ""

        # ---- leg 1: generalize (bound-constant re-parameterization) ----
        def _leg_generalize() -> Any:
            from swarm_engine.acquisition.generalize_driver import (
                generalize_for_task)
            return generalize_for_task(
                self.engine, self.epistemic, technique_ref, novel_goal,
                list(train_examples) + list(held_out))

        gres, gerr = _run_leg("probe:generalize", _leg_generalize)
        if gerr == "budget_exhausted":
            env_id = self._record_envelope(
                technique_ref=technique_ref, novel_goal=novel_goal,
                mechanism="generalize", outcome="budget_exhausted",
                heldout="", capability_id="", composed_of=[],
                reason="generalize leg exhausted its budget",
                root_mc_id=root_id, leg_mc_ids=leg_ids,
                holds=[], breaks=[])
            return _finish("budget_exhausted", "generalize",
                           reason="generalize leg exhausted its budget",
                           envelope_observation_id=env_id)
        if gerr:
            env_id = self._record_envelope(
                technique_ref=technique_ref, novel_goal=novel_goal,
                mechanism="generalize", outcome="error",
                heldout="", capability_id="", composed_of=[],
                reason=gerr, root_mc_id=root_id, leg_mc_ids=leg_ids,
                holds=[], breaks=[])
            return _finish("error", "generalize", reason=gerr,
                           envelope_observation_id=env_id)
        if isinstance(gres, Exception):
            env_id = self._record_envelope(
                technique_ref=technique_ref, novel_goal=novel_goal,
                mechanism="generalize", outcome="error",
                heldout="", capability_id="", composed_of=[],
                reason=f"{type(gres).__name__}: {gres}",
                root_mc_id=root_id, leg_mc_ids=leg_ids,
                holds=[], breaks=[])
            return _finish("error", "generalize",
                           reason=f"{type(gres).__name__}: {gres}",
                           envelope_observation_id=env_id)
        if gres.success:
            held = f"{gres.heldout_passed}/{gres.heldout_examples}"
            env_id = self._record_envelope(
                technique_ref=technique_ref, novel_goal=novel_goal,
                mechanism="generalize", outcome="crossed",
                heldout=held, capability_id=gres.capability_id or "",
                composed_of=[], reason="",
                root_mc_id=root_id, leg_mc_ids=leg_ids,
                holds=[f"bound-constant re-parameterization to: {novel_goal}"],
                breaks=[])
            return _finish(
                "crossed", "generalize", heldout=held,
                capability_id=gres.capability_id or "",
                promoted_name=gres.promoted_name or "",
                envelope_observation_id=env_id,
                reason="generalized via DistillationLoop.generalize "
                       "(ReviewBoard + verdict path inside the machinery)")

        # ---- leg 2: compose (planner-level composition) ----
        def _leg_compose() -> Any:
            from swarm_engine.synthesis.plan_composer import (
                PlanComposer, CompositionObjective)
            from swarm_engine.synthesis.compose_inlet import q8_authenticate
            from swarm_engine.synthesis.admission import SmokeTest, Verdict

            pc = PlanComposer(self.engine.composer)
            objective = CompositionObjective(
                goal=novel_goal, gap_id=gap, params=dict(params),
                output_kind=output_kind,
                examples=list(train_examples),
                held_out=list(held_out))
            res = pc.compose(objective)
            if not res.found or not res.plan:
                return {"status": "no_plan",
                        "evaluated": res.candidates_evaluated,
                        "exhausted": res.search_exhausted,
                        "composed_of": list(res.composed_of)}
            # Verify through the Q8 inlet -- never weakened, never skipped.
            q8ok, q8_reasons = q8_authenticate(
                res.plan, self.engine.composer,
                list(train_examples), list(held_out))
            if not q8ok:
                return {"status": "q8_refused",
                        "reasons": list(q8_reasons),
                        "composed_of": list(res.composed_of)}
            # Causal contrast: the distilled technique must be necessary.
            regs = self._technique_registrations(technique_ref)
            contrast = CompositionObjective(
                goal=novel_goal, gap_id=gap + ":contrast",
                params=dict(params), output_kind=output_kind,
                examples=list(train_examples),
                held_out=list(held_out), forbidden=tuple(regs))
            cres = pc.compose(contrast)
            if cres.found:
                return {"status": "contrast_failed",
                        "composed_of": list(res.composed_of),
                        "contrast_composed_of": list(cres.composed_of)}
            # Admit through the governed path only.
            plan = dict(res.plan)
            plan["provenance"] = {
                "composed_of": list(res.composed_of),
                "method": "gen-ctrl-1:compose",
                "gap_id": gap,
            }
            smoke_args = dict(train_examples[0][0]) if train_examples else {}
            smoke_expect = train_examples[0][1] if train_examples else None
            adm = self.engine.admit_as_engine(
                novel_goal, plan,
                smoke=SmokeTest(args=smoke_args, expect=smoke_expect),
                name=admission_name or "genctrl1_composed",
            )
            if not (adm.ok and adm.verdict == Verdict.ADMITTED):
                return {"status": "admission_refused",
                        "composed_of": list(res.composed_of),
                        "detail": f"ok={adm.ok} verdict={adm.verdict}"}
            return {"status": "crossed",
                    "capability_id": adm.capability_id,
                    "composed_of": list(res.composed_of),
                    "evaluated": res.candidates_evaluated}

        cres_out, cerr = _run_leg("probe:compose", _leg_compose)
        if cerr == "budget_exhausted":
            env_id = self._record_envelope(
                technique_ref=technique_ref, novel_goal=novel_goal,
                mechanism="compose", outcome="budget_exhausted",
                heldout="", capability_id="", composed_of=[],
                reason="compose leg exhausted its budget",
                root_mc_id=root_id, leg_mc_ids=leg_ids,
                holds=[], breaks=[])
            return _finish("budget_exhausted", "compose",
                           reason="compose leg exhausted its budget",
                           envelope_observation_id=env_id)
        if cerr or isinstance(cres_out, Exception):
            reason = cerr or f"{type(cres_out).__name__}: {cres_out}"
            env_id = self._record_envelope(
                technique_ref=technique_ref, novel_goal=novel_goal,
                mechanism="compose", outcome="error",
                heldout="", capability_id="", composed_of=[],
                reason=reason, root_mc_id=root_id, leg_mc_ids=leg_ids,
                holds=[], breaks=[])
            return _finish("error", "compose", reason=reason,
                           envelope_observation_id=env_id)

        status = cres_out.get("status", "error")
        composed_of = cres_out.get("composed_of", [])
        if status == "crossed":
            cap_id = cres_out["capability_id"]
            held = f"{len(held_out)}/{len(held_out)}"
            env_id = self._record_envelope(
                technique_ref=technique_ref, novel_goal=novel_goal,
                mechanism="compose", outcome="crossed",
                heldout=held, capability_id=cap_id,
                composed_of=composed_of, reason="",
                root_mc_id=root_id, leg_mc_ids=leg_ids,
                holds=[f"planner-level composition over distilled "
                       f"{technique_ref.get('promoted_name')}: {novel_goal}"],
                breaks=[])
            return _finish(
                "crossed", "compose", heldout=held,
                capability_id=cap_id, envelope_observation_id=env_id,
                reason=f"composed {composed_of} in "
                       f"{cres_out.get('evaluated')} evaluations; Q8 "
                       f"authenticated; causal contrast clean; admitted")
        # Honest failure: mark the bound, never admit.
        breaks = []
        if status == "no_plan":
            breaks = [f"composer search exhausted "
                      f"({cres_out.get('evaluated')} evaluations): "
                      f"{novel_goal} is outside the searchable plan space"]
            reason = "composer honestly found no plan"
        elif status == "q8_refused":
            breaks = [f"Q8 authentication refused the composed plan: "
                      f"{'; '.join(cres_out.get('reasons', []))}"]
            reason = "Q8 refused the composed plan"
        elif status == "contrast_failed":
            breaks = ["causal contrast failed: the task is solvable without "
                      "the distilled technique; no generalization crossing "
                      "claimed"]
            reason = "causal contrast failed"
        elif status == "admission_refused":
            breaks = [f"admit_as_engine refused: {cres_out.get('detail')}"]
            reason = "admission refused"
        else:
            reason = f"unexpected compose status: {status}"
            breaks = [reason]
        # The generalize leg's named refusal is part of the bound record.
        g_reason = getattr(gres, "reason", "") or ""
        if g_reason:
            breaks.append(f"generalize leg refused: {g_reason}")
        env_id = self._record_envelope(
            technique_ref=technique_ref, novel_goal=novel_goal,
            mechanism="compose", outcome="bound_marked",
            heldout="", capability_id="", composed_of=composed_of,
            reason=reason, root_mc_id=root_id, leg_mc_ids=leg_ids,
            holds=[], breaks=breaks)
        return _finish("bound_marked", "compose", reason=reason,
                       envelope_observation_id=env_id)

    # ------------------------------------------------------------------ helpers

    def _technique_registrations(
            self, technique_ref: Dict[str, Any]) -> List[str]:
        """Planner registrations of the distilled technique.

        The distillation pipeline registers a technique twice (dual
        registration): ``acquired.{capability_id}`` via admit_as_engine
        and ``acquired.{promoted_name}`` via verdict promotion. The
        promoted name may already carry the ``acquired.`` prefix (as
        ``dres.promoted_name`` does), so the prefix is normalized, never
        doubled -- a doubled prefix silently forbids nothing and would
        make the causal contrast vacuous. Only names actually present in
        the registry are returned: the contrast forbids exactly the
        technique's registrations, nothing else.
        """
        reg = self.engine.composer.reg
        cands: List[str] = []
        for base in (technique_ref.get("capability_id") or "",
                     technique_ref.get("promoted_name") or ""):
            if not base:
                continue
            cands.append(base if base.startswith("acquired.")
                         else f"acquired.{base}")
        seen = set()
        out = []
        for n in cands:
            if n not in seen and reg.get(n) is not None:
                seen.add(n)
                out.append(n)
        return out
