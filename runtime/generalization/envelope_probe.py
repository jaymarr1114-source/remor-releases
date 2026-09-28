"""GEN-ENV-1: envelope-probing automation on the Generalization Controller.

The automation owns three things and nothing else:

1. **Enumerate** variant tasks around a distilled technique:
   - *sweep arm*: read the technique's own bound constants (from its
     retained capability record, the same read-only recovery the
     distillation loop itself uses) and emit one variant per
     constant x alternative target;
   - *structural / control arms*: caller-supplied variants (the
     automation cannot invent task semantics from nothing; the shapes
     encode current mechanism knowledge, and at least one control is
     a shape known to be outside the machinery).
2. **Drive** every variant through the controller's
   ``probe -> verify -> expand/mark`` cycle (``run_cycle``), in a fixed
   order, with no hand in the middle.
3. **Publish** the resulting envelope: per-cycle records are written by
   the controller itself; the automation adds one summary report record
   through the same unified write path, so the published envelope is
   baseline + per-cycle observations + the report.

The automation never reimplements the controller, the composer,
distillation, generalization, admission, or the envelope store: it calls
``GeneralizationController.run_cycle`` and reads back through
``GeneralizationController.envelope_for``. It never weakens Q8
authentication, the causal contrast, or the composer's honest exhaustion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from swarm_engine.generalization.controller import (
    ENVELOPE_KIND,
    ORIGIN_LOOP,
    GeneralizationController,
)

ENVELOPE_REPORT_MARKER = "envelope_probe_report"

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


# ------------------------------------------------------------------ variants

@dataclass
class VariantSpec:
    """One envelope-probing variant task."""
    name: str
    novel_goal: str
    train_examples: List[Tuple[Dict[str, Any], Any]]
    held_out: List[Tuple[Dict[str, Any], Any]]
    expected: str  # "crossed" | "bound" (the automation's hypothesis)
    arm: str       # "sweep" | "structural" | "control"
    target_constant: Optional[float] = None


def technique_bound_constants(engine: Any,
                              technique_ref: Dict[str, Any]
                              ) -> Tuple[List[float], str]:
    """Numeric constants bound in the technique's own distilled plan.

    Read-only walk over the retained capability record's plan steps:
    numeric step arguments are the technique's bound constants (reference
    dicts like ``{"$param": "x"}`` / ``{"$step": "s1"}`` are skipped).
    This deliberately reads the plan structure, not rendered source text
    -- the rendered text embeds the plan JSON in a header comment, so a
    text scan picks up example values and rendering artifacts as phantom
    "constants" (observed: square's rendering carries 0.0, 1.0, 2.0,
    10.0, 16.0, 18.0, 1e+18, none of them bound). An empty list is an
    honest answer (constant-free technique: the sweep arm does not
    apply), never an error.
    """
    cap_id = technique_ref.get("capability_id") or ""
    if not cap_id:
        return [], "technique_ref carries no capability_id"
    try:
        rec = engine.capabilities.get(cap_id)
        plan = getattr(rec, "plan", None)
        if not plan:
            return [], f"no retained plan for {cap_id}"
    except Exception as exc:  # noqa: BLE001 -- honest empty, not a crash
        return [], f"plan recovery failed: {type(exc).__name__}: {exc}"
    consts = set()

    def walk(v: Any) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            consts.add(float(v))
            return
        if isinstance(v, dict):
            keys = set(v.keys())
            if keys <= {"$param", "$step"}:
                return
            for k, vv in v.items():
                if isinstance(k, str) and k.startswith("$"):
                    continue
                walk(vv)
        elif isinstance(v, (list, tuple)):
            for vv in v:
                walk(vv)

    walk(plan.get("steps", []))
    vals = sorted(consts)
    if not vals:
        return [], "plan binds no numeric constants"
    return vals, f"bound constants: {vals}"


def generate_sweep_variants(
    technique_ref: Dict[str, Any],
    engine: Any,
    goal_template: str,
    example_oracle: Callable[[float],
                             Tuple[List[Tuple[Dict[str, Any], Any]],
                                     List[Tuple[Dict[str, Any], Any]]]],
    sweep_values: Optional[List[float]] = None,
) -> Tuple[List[VariantSpec], str]:
    """Bound-constant sweep variants for one technique.

    For each numeric literal bound in the technique's code, emit one
    variant per alternative target value. ``goal_template`` is formatted
    with ``target=`` (it must name exactly one distinct number -- the
    generalize leg's ``_goal_number`` requirement); ``example_oracle``
    maps a target to (train, held_out). The hypothesis for every sweep
    variant is "crossed": bound-constant re-parameterization is the
    generalize leg's job. A constant-free technique yields ([], note).
    """
    consts, note = technique_bound_constants(engine, technique_ref)
    if not consts:
        return [], f"sweep arm not applicable: {note}"
    variants: List[VariantSpec] = []
    for old in consts:
        targets = (list(sweep_values) if sweep_values is not None
                   else [old + 2, old * 2])
        for t in targets:
            if t == old:
                continue
            train, held = example_oracle(t)
            variants.append(VariantSpec(
                name=f"sweep_{old:g}_to_{t:g}",
                novel_goal=goal_template.format(target=t),
                train_examples=train, held_out=held,
                expected="crossed", arm="sweep",
                target_constant=float(t)))
    return variants, (f"sweep arm: {len(variants)} variants from {note}")


# ------------------------------------------------------------------ prober

@dataclass
class VariantOutcome:
    spec: VariantSpec
    outcome: str
    mechanism: str
    capability_id: Optional[str]
    promoted_name: str
    heldout: str
    reason: str
    envelope_observation_id: Optional[str]
    retired_clean: bool
    agreed: Optional[bool] = None  # set by verify_agreement


@dataclass
class ProbeReport:
    technique_promoted_name: str
    outcomes: List[VariantOutcome] = field(default_factory=list)
    report_observation_id: str = ""
    sweep_note: str = ""

    @property
    def expansions(self) -> List[VariantOutcome]:
        return [o for o in self.outcomes if o.outcome == "crossed"]

    @property
    def bounds(self) -> List[VariantOutcome]:
        return [o for o in self.outcomes if o.outcome == "bound_marked"]

    @property
    def errors(self) -> List[VariantOutcome]:
        return [o for o in self.outcomes
                if o.outcome not in ("crossed", "bound_marked")]


class EnvelopeProber:
    """Drives envelope-probing variants through the controller."""

    def __init__(self, controller: GeneralizationController) -> None:
        self.controller = controller

    def probe_technique(
        self,
        technique_ref: Dict[str, Any],
        variants: List[VariantSpec],
        *,
        params: Dict[str, Any],
        output_kind: Any,
        gap_prefix: str,
        admission_prefix: str,
        sweep_note: str = "",
    ) -> ProbeReport:
        """Run every variant through run_cycle; publish the envelope report."""
        report = ProbeReport(
            technique_promoted_name=technique_ref.get("promoted_name", ""),
            sweep_note=sweep_note)
        for spec in variants:
            cycle = self.controller.run_cycle(
                technique_ref=technique_ref,
                novel_goal=spec.novel_goal,
                train_examples=spec.train_examples,
                held_out=spec.held_out,
                params=dict(params), output_kind=output_kind,
                gap_id=f"{gap_prefix}:{spec.name}",
                admission_name=f"{admission_prefix}_{spec.name}")
            report.outcomes.append(VariantOutcome(
                spec=spec, outcome=cycle.outcome,
                mechanism=cycle.mechanism,
                capability_id=cycle.capability_id,
                promoted_name=cycle.promoted_name or "",
                heldout=cycle.heldout, reason=cycle.reason,
                envelope_observation_id=cycle.envelope_observation_id,
                retired_clean=cycle.retired_clean))
        report.report_observation_id = self._publish_report(
            technique_ref, report)
        return report

    def _publish_report(self, technique_ref: Dict[str, Any],
                        report: ProbeReport) -> str:
        from swarm_engine.intellect.unified_memory import record_experience
        raw_variants = [{
            "name": o.spec.name, "arm": o.spec.arm,
            "expected": o.spec.expected, "outcome": o.outcome,
            "mechanism": o.mechanism, "heldout": o.heldout,
            "capability_id": o.capability_id,
            "promoted_name": o.promoted_name, "reason": o.reason,
            "envelope_observation_id": o.envelope_observation_id,
            "target_constant": o.spec.target_constant,
        } for o in report.outcomes]
        return record_experience(
            self.controller.epistemic,
            origin_loop=ORIGIN_LOOP,
            kind=ENVELOPE_KIND,
            content=(f"envelope probe report: "
                     f"{technique_ref.get('promoted_name')} -- "
                     f"{len(report.expansions)} expansions, "
                     f"{len(report.bounds)} marked bounds, "
                     f"{len(report.errors)} errors "
                     f"over {len(report.outcomes)} variants"),
            raw={
                "technique_promoted_name":
                    technique_ref.get("promoted_name"),
                "technique_capability_id":
                    technique_ref.get("capability_id"),
                ENVELOPE_REPORT_MARKER: True,
                "sweep_note": report.sweep_note,
                "variants": raw_variants,
                "expansions": [o.spec.name for o in report.expansions],
                "bounds": [o.spec.name for o in report.bounds],
                "errors": [o.spec.name for o in report.errors],
            },
            source="generalization/envelope_probe",
        )

    def published_envelope(self, promoted_name: str,
                           limit: int = 200) -> Dict[str, Any]:
        """The published envelope: baseline + per-cycle records + reports."""
        return self.controller.envelope_for(promoted_name, limit=limit)


def verify_agreement(prober: EnvelopeProber,
                     technique_ref: Dict[str, Any],
                     spec: VariantSpec,
                     recorded_outcome: str,
                     *,
                     params: Dict[str, Any],
                     output_kind: Any) -> Tuple[bool, str]:
    """Independently re-probe one published variant; compare outcomes.

    A fresh run_cycle (new microcontrollers, new envelope observation --
    an honest duplicate observation, not a cache hit) must agree with the
    published record. One legitimate exception: re-probing a
    generalize-leg crossing re-derives byte-identical code, and the
    promotion registry is write-once, so promotion is refused as
    already-registered while the mechanism itself reproduced. That case
    is agreement-with-idempotence, detected from the re-probe's own
    envelope record (its breaks name "promotion refused (fail closed)"),
    never asserted blindly. Any other disagreement is a defect in the
    automation.
    """
    cycle = prober.controller.run_cycle(
        technique_ref=technique_ref,
        novel_goal=spec.novel_goal,
        train_examples=spec.train_examples,
        held_out=spec.held_out,
        params=dict(params), output_kind=output_kind,
        gap_id=f"agreement:{spec.name}",
        admission_name=f"agreement_{spec.name}")
    if cycle.outcome == recorded_outcome:
        return True, (f"published={recorded_outcome} "
                      f"reprobe={cycle.outcome}/{cycle.mechanism}")
    recs = prober.published_envelope(
        technique_ref.get("promoted_name", ""))["technique_records"]
    mine = [r for r in recs
            if (r.get("raw") or {}).get("novel_goal") == spec.novel_goal]
    mine.sort(key=lambda r: r.get("at") or 0)
    latest_raw = (mine[-1].get("raw") or {}) if mine else {}
    breaks = latest_raw.get("breaks") or []
    if (recorded_outcome == "crossed"
            and cycle.outcome == "bound_marked"
            and any("promotion refused (fail closed)" in b for b in breaks)):
        return True, (f"agreement-with-idempotence: generalize leg "
                      f"reproduced the substitution but promotion refused "
                      f"already-registered bytes; published={recorded_outcome} "
                      f"reprobe={cycle.outcome}")
    return False, (f"published={recorded_outcome} "
                    f"reprobe={cycle.outcome}/{cycle.mechanism}")
