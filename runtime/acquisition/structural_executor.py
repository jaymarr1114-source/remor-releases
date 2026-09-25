"""
Structural representation acquisition executor (M+10–M+17).

M+17: convert structural candidates to executable $partial and independently
validate them through Composer against behavioral examples.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass
class StructuralAcquisitionAttempt:
    executed: bool = True
    acquired: bool = False
    target: Optional[Dict[str, Any]] = None
    required_kind: Optional[str] = None
    required_for: Optional[str] = None
    constructible: Optional[bool] = None
    reason: str = ""
    candidates: List[Any] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    validation_results: List[Dict[str, Any]] = field(default_factory=list)
    validated_candidates: List[Any] = field(default_factory=list)
    admission_results: List[Dict[str, Any]] = field(default_factory=list)
    admitted_ids: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        cand = []
        for c in self.candidates:
            cand.append(c.as_dict() if hasattr(c, "as_dict") else c)
        return {
            "executed": self.executed,
            "acquired": self.acquired,
            "target": self.target,
            "required_kind": self.required_kind,
            "required_for": self.required_for,
            "constructible": self.constructible,
            "reason": self.reason,
            "candidates_generated": len(self.candidates),
            "candidates": cand[:20],
            "notes": list(self.notes),
            "route": "structural_executor",
            "validation_results": list(self.validation_results),
            "validated_count": len(self.validated_candidates),
            "admission_results": list(self.admission_results)[:20],
            "admitted_ids": list(self.admitted_ids),
            "admitted_count": len(self.admitted_ids),
        }


def _items_param(examples: Sequence[Tuple[Dict[str, Any], Any]]) -> str:
    if not examples:
        return "values"
    keys = list(examples[0][0].keys())
    return keys[0] if keys else "values"


def validate_callable_candidate(
        candidate: Any,
        examples: Sequence[Tuple[Dict[str, Any], Any]],
        composer: Any,
) -> Dict[str, Any]:
    """Independently execute candidate as map.fn via Composer $partial.

    Causal direction: candidate → $partial plan → Composer → observed output
    → compare to expected. Does not use $lambda or Python bridges.
    """
    from swarm_engine.acquisition.structural_candidates import (
        to_partial_form, map_plan_with_partial,
    )
    result: Dict[str, Any] = {
        "candidate": candidate.as_dict() if hasattr(candidate, "as_dict") else candidate,
        "passed": False,
        "partial_form": None,
        "cases": [],
        "error": None,
    }
    try:
        partial = to_partial_form(candidate)
        result["partial_form"] = partial
        # Reject if somehow lambda
        if "$lambda" in str(partial):
            result["error"] = "lambda forbidden in structural validation"
            return result
        param = _items_param(examples)
        plan = map_plan_with_partial(candidate, items_param=param)
        if "$lambda" in str(plan):
            result["error"] = "lambda forbidden in plan"
            return result
        all_ok = True
        for args, expected in examples:
            out = composer.execute_sync(plan, dict(args))
            case = {
                "args": dict(args),
                "expected": expected,
                "success": bool(out.get("success")),
                "value": out.get("value"),
                "error": out.get("error"),
            }
            result["cases"].append(case)
            if not out.get("success") or out.get("value") != expected:
                all_ok = False
        result["passed"] = all_ok and bool(examples)
    except Exception as ex:
        result["error"] = f"{type(ex).__name__}: {ex}"
        result["passed"] = False
    return result


class StructuralRepresentationExecutor:
    """Entrypoint for Strategy.STRUCTURAL."""

    def __init__(self, registry=None, composer=None, admission=None):
        self.registry = registry
        self.composer = composer
        self.admission = admission

    def execute(self, spec: Any) -> StructuralAcquisitionAttempt:
        target = dict(getattr(spec, "acquisition_target", None) or {})
        if target.get("class") != "structural_representation_capability":
            return StructuralAcquisitionAttempt(
                executed=False,
                reason="spec.acquisition_target is not structural_representation_capability",
                target=target or None,
            )
        required_kind = target.get("required_kind") or "CALLABLE"
        required_for = target.get("required_for")
        constructible = target.get("constructible_by_current_search")
        examples = list(getattr(spec, "examples", None) or [])

        candidates: List[Any] = []
        notes = [
            "M+17 structural candidate → $partial → independent validation",
            "no $lambda",
            "no task-keyword mapping",
        ]
        reg = self.registry
        if reg is None:
            notes.append("no registry supplied; cannot generate candidates")
        else:
            from swarm_engine.acquisition.structural_candidates import (
                StructuralCallableCandidateGenerator,
            )
            gen = StructuralCallableCandidateGenerator(reg)
            candidates = gen.generate(
                required_kind=str(required_kind), examples=examples)
            notes.append(
                f"generated {len(candidates)} CALLABLE structural candidates")

        validation_results: List[Dict[str, Any]] = []
        validated: List[Any] = []
        if self.composer is not None and examples and candidates:
            # Cap validation work for determinism
            for cand in candidates:
                vr = validate_callable_candidate(cand, examples, self.composer)
                validation_results.append(vr)
                if vr.get("passed"):
                    validated.append(cand)
            notes.append(
                f"validated {len(validation_results)} candidates; "
                f"{len(validated)} passed behavioral examples")
        elif not examples:
            notes.append("no examples; validation skipped")
        elif self.composer is None:
            notes.append("no composer; validation skipped")

        admission_results: List[Dict[str, Any]] = []
        admitted_ids: List[str] = []
        # Prefer user-facing source_goal when present (M+20); fall back to
        # description/name. Avoids binding only internal gap text.
        _at = getattr(spec, "acquisition_target", None) or {}
        goal = (_at.get("source_goal")
                or getattr(spec, "description", None)
                or getattr(spec, "name", "")
                or "structural_capability")
        if self.admission is not None and validated and examples:
            from swarm_engine.acquisition.structural_candidates import map_plan_with_partial
            from swarm_engine.synthesis.admission import SmokeTest
            param = _items_param(examples)
            # Admit at most a few validated candidates to avoid registry flood
            for cand in validated[:5]:
                try:
                    plan = map_plan_with_partial(cand, items_param=param)
                    smoke = SmokeTest(args=dict(examples[0][0]),
                                      expect=examples[0][1])
                    name = f"structural_{cand.op}_" + "_".join(
                        f"{k}{v}" for k, v in sorted((cand.bound or {}).items()))
                    verdict = self.admission.admit(
                        goal=str(goal), plan=plan, smoke=smoke, name=name[:80])
                    admission_results.append({
                        "candidate": cand.as_dict() if hasattr(cand, "as_dict") else cand,
                        "verdict": verdict.verdict,
                        "capability_id": verdict.capability_id,
                        "stage": verdict.stage,
                        "reasons": list(verdict.reasons),
                        "ok": verdict.ok,
                    })
                    if verdict.ok and verdict.capability_id:
                        admitted_ids.append(verdict.capability_id)
                        notes.append(
                            f"admitted {verdict.capability_id} via AdmissionController")
                except Exception as ex:
                    admission_results.append({
                        "candidate": cand.as_dict() if hasattr(cand, "as_dict") else cand,
                        "verdict": "error",
                        "ok": False,
                        "reasons": [f"{type(ex).__name__}: {ex}"],
                    })
            notes.append(
                f"admission attempted for {len(admission_results)}; "
                f"admitted {len(admitted_ids)}")
        elif validated and self.admission is None:
            notes.append("validation passed but no admission controller supplied")

        if admitted_ids:
            reason = (
                f"structural admission: {len(admitted_ids)} capability(ies) admitted "
                f"via governed AdmissionController for {required_for}"
            )
        elif validated:
            reason = (
                f"structural validation: {len(validated)} candidate(s) passed "
                f"behavioral examples via $partial/Composer for {required_for}; "
                f"admission not completed"
            )
        elif candidates:
            reason = (
                f"structural candidate generation produced {len(candidates)} "
                f"candidates; none passed independent behavioral validation "
                f"for {required_for}"
            )
        else:
            reason = (
                "no CALLABLE structural candidates could be derived from pure "
                "primitives"
            )

        return StructuralAcquisitionAttempt(
            executed=True,
            acquired=bool(admitted_ids),
            target=target,
            required_kind=required_kind,
            required_for=required_for,
            constructible=constructible,
            reason=reason,
            candidates=candidates,
            notes=notes,
            validation_results=validation_results,
            validated_candidates=validated,
            admission_results=admission_results,
            admitted_ids=admitted_ids,
        )
