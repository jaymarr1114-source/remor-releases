"""
swarm_engine/cognition/grounding_assets.py

Learned grounding programs as first-class reusable acquisition assets.

The precise wiring gap this closes: GroundingLexicon.learn() discovers a
VERIFIED executable program (an Expr over generic arithmetic, validated by
governance against world-observed evidence and persisted), but the program
lives in the lexicon's own store -- invisible to AcquisitionOrchestrator,
which only sees the engine's capability store and primitive registry.
make_objective() transfers only *examples* across that bridge, so every
acquisition REDISCOVERS the law from scratch through the orchestrator's
weaker synthesizer. Laws the lexicon's high-completeness synthesizer can
learn (3-property laws) then die as SEARCH-RESOURCE CONSTRAINED at the
acquisition layer, even though the verified program already exists.

This module is the honest bridge, and it creates NO second capability
system:

  learned Expr (verified, governed, persisted by the lexicon)
    -> compiled to an engine plan (mechanical Expr->plan translation over
       the shared primitive vocabulary; no predicate/entity knowledge)
    -> smoke-tested against the lexicon's retained world evidence
       (verifies the TRANSLATION; the law itself was verified at learn)
    -> admitted through the engine's NORMAL AdmissionController.admit()
       (type/effect/permission checks, fingerprint-reuse, corruption
       quarantine -- nothing bypassed)
    -> registered as acquired.<capability_id> primitive by the existing
       M+29.29 machinery, persisted in the engine DB, rehydrated by the
       existing rehydrate_acquired() path.

From there, ALL downstream reuse is existing production machinery, not
new code: the gap reasoner's behavioral discovery (find_compatible)
matches admitted capabilities against a node's worked examples;
_record_acquired_primitive bridges discovered children to parents;
_try_pair_assembly composes. The orchestrator needs no changes.

Two export granularities, both driven by structural metadata, never by
test knowledge:

  1. export_grounding_capability(lexicon, engine, predicate):
     role-keyed params (a0_/a1_) -- for flat single-predicate objectives
     whose examples are role-keyed (make_objective's own output shape).

  2. export_compound_grounding_capabilities(compound_objective, lexicon,
     engine): one export per distinct (predicate, agent_slot,
     patient_slot) wiring from the compound's clause signature. The
     role->slot renaming (a0_X -> s{agent}_X) is pure alpha-renaming of
     input keys, derived from the structure's slot assignment. Compound
     child examples are slot-keyed, so without the renaming the admitted
     program could never behaviorally match (representation mismatch,
     not a capability defect).

Anti-simulation: no predicate token is ever named, branched on, or
looked up in this module -- predicates arrive as opaque strings from
the caller (the lexicon groups by them; the compound objective carries
them in its structural wirings). No expected programs, no capability
IDs, no test-side executables. A predicate with no usable learned entry
is skipped (fail closed), never invented.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROLE_PREFIXES = ("a0_", "a1_")


def _rename_param(name: str, slot_map: Optional[Dict[int, int]]) -> str:
    """Alpha-rename a role-keyed param (a0_X / a1_X) to a slot-keyed one
    (s{slot}_X) using the clause's role->slot assignment. slot_map maps
    role index (0/1) -> slot index. None means no renaming."""
    if not slot_map:
        return name
    for prefix in _ROLE_PREFIXES:
        if name.startswith(prefix):
            role = int(prefix[1])
            slot = slot_map.get(role)
            if slot is None:
                raise ValueError(
                    f"role a{role} has no slot assignment in {slot_map}")
            return f"s{slot}_{name[len(prefix):]}"
    raise ValueError(f"param {name!r} is outside the a0_/a1_ role convention")


def compile_grounding_plan(expr: Any, schema: Sequence[str],
                           registry: Any,
                           slot_map: Optional[Dict[int, int]] = None
                           ) -> Dict[str, Any]:
    """Compile a learned grounding Expr to an engine plan dict.

    Mechanical and generic: every op node becomes one plan step calling
    the same-named engine primitive (the lexicon's GROUND_ARITH_OPS are a
    subset of the engine registry's names -- verified here, fail closed
    if any op is absent); param leaves become $param refs (renamed via
    slot_map when given); literal leaves become bare literal args, which
    the composer treats as literal values. Child arg names are checked
    against the primitive's declared inputs so a mistranslation fails
    here, not silently at execution.
    """
    steps: List[Dict[str, Any]] = []
    counter = [0]

    def ref_of(e: Any) -> Any:
        if e.is_leaf():
            if e.is_literal:
                return e.literal
            return {"$param": _rename_param(e.param, slot_map)}
        prim = registry.get(e.op)
        if prim is None:
            raise ValueError(
                f"grounding op {e.op!r} is not a registered engine "
                f"primitive; refusing to compile an unexecutable plan")
        declared = set(prim.inputs.keys())
        args: Dict[str, Any] = {}
        for child_name, child in e.children:
            if child_name not in declared:
                raise ValueError(
                    f"arg {child_name!r} is not a declared input of "
                    f"primitive {e.op!r}")
            args[child_name] = ref_of(child)
        sid = f"g{counter[0]}"
        counter[0] += 1
        steps.append({"id": sid, "op": e.op, "args": args})
        return {"$step": sid}

    output = ref_of(expr)
    params = {_rename_param(p, slot_map): "any" for p in schema}
    return {"name": "grounding_program", "params": params,
            "steps": steps, "output": output}


def _rename_evidence(evidence: Sequence[Dict[str, Any]],
                     slot_map: Optional[Dict[int, int]]
                     ) -> List[Tuple[Dict[str, Any], Any]]:
    """Rename retained world evidence inputs through the same slot map so
    the smoke test verifies the RENAMED artifact, not the original."""
    out: List[Tuple[Dict[str, Any], Any]] = []
    for ev in evidence:
        if not ev.get("consistent", True):
            continue
        inputs = { _rename_param(k, slot_map): v
                   for k, v in ev["inputs"].items() }
        out.append((inputs, ev["output"]))
    return out


def _notify_reifier(engine: Any, entry: Any, predicate: str,
                    description: str) -> Dict[str, Any]:
    """Route a verified learned law to the engine's ReificationObserver.

    The observer + CaseMemory previously only ever saw cognition
    syntheses; grounding learn() was never routed through them. A learned
    law is a verified (goal, expr, examples) triple like any other, so
    record the canonical role-keyed law as a case and let the observer
    detect cross-goal recurrence autonomously -- no manual promotion
    requests, no goal-text routing.

    The case is attributed to its governing law (predicate, semantic_id)
    so promotions derived from it carry epistemic provenance: if later
    evidence contradicts that law, the revocation sync can assess exactly
    which promotions lost their justification.

    Fail-closed: any problem here is reported, never raised into the
    export path that already succeeded.
    """
    report: Dict[str, Any] = {"reified": []}
    try:
        learner = getattr(getattr(engine, "cognition", None), "learner", None)
        if learner is None:
            return report
        expr = entry.expr
        ev = [(dict(c["inputs"]), c["output"])
              for c in (entry.evidence or [])
              if isinstance(c, dict) and c.get("consistent", True)
              and isinstance(c.get("inputs"), dict)]
        if expr is None or not ev:
            return report
        params = sorted({p for a, _ in ev for p in a.keys()})
        learner.cases.remember(
            description, expr.ops_used(), {"params": params, "steps": []},
            expr=expr, examples=ev, param_names=params,
            law={"predicate": predicate,
                 "semantic_id": getattr(entry, "semantic_id", None)})
        reifier = getattr(learner, "reifier", None)
        if reifier is None:
            report["reifier"] = "absent"
            return report
        promoted = reifier.observe(expr, description, ev, params)
        report["reified"] = list(promoted or [])
        return report
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design
        report["reify_error"] = f"{type(exc).__name__}: {exc}"
        return report


def export_grounding_capability(lexicon: Any, engine: Any, predicate: str,
                               goal_text: str = "",
                               slot_map: Optional[Dict[int, int]] = None,
                               max_smoke_cases: int = 8) -> Dict[str, Any]:
    """Admit one learned grounding program to the engine as a reusable
    capability. Returns a report dict; never raises on expected
    fail-closed conditions (unknown/unusable predicate, uncompilable
    program, no verifiable evidence, admission refusal).

    The report carries capability_id and primitive_name on success so
    callers can assert reuse causally (e.g. the parent plan must call
    the primitive by name), without the test ever selecting the program.

    After successful admission, the verified learned law is also routed
    to the engine's ReificationObserver (fail-closed), so recurring
    grounding structure across distinct goals can earn autonomous
    promotion into new primitives.
    """
    entry = lexicon.get(predicate) if lexicon is not None else None
    if entry is None:
        return {"ok": False, "predicate": predicate,
                "reason": "unknown predicate: no lexicon entry"}
    if not entry.usable():
        return {"ok": False, "predicate": predicate,
                "reason": f"entry not usable (status={entry.status})"}
    if slot_map:
        try:
            for p in entry.input_schema:
                _rename_param(p, slot_map)
        except ValueError as exc:
            return {"ok": False, "predicate": predicate,
                    "reason": f"schema outside role convention: {exc}"}
    try:
        plan = compile_grounding_plan(entry.expr, entry.input_schema,
                                      engine.primitives, slot_map)
    except ValueError as exc:
        return {"ok": False, "predicate": predicate,
                "reason": f"compilation refused: {exc}"}
    plan["name"] = _capability_name(predicate, slot_map)

    # Verify the TRANSLATED artifact against retained world evidence
    # (renamed through the same map). The law's correctness was
    # established at learn() time by governance; this checks that the
    # compilation preserved it.
    smoke_cases = _rename_evidence(entry.evidence, slot_map)[:max_smoke_cases]
    if not smoke_cases:
        return {"ok": False, "predicate": predicate,
                "reason": "no consistent retained evidence to verify against"}
    bad = []
    for args, expected in smoke_cases:
        try:
            run = engine.composer.execute_sync(plan, dict(args),
                                               skip_check=True)
        except Exception as exc:  # noqa: BLE001 -- fail closed, reported
            bad.append((args, f"raised {type(exc).__name__}"))
            break
        if not run.get("success") or run.get("value") != expected:
            bad.append((args, run.get("value"), expected,
                        run.get("error")))
            break
    if bad:
        return {"ok": False, "predicate": predicate,
                "reason": f"compiled plan failed evidence replay: {bad[:1]}"}

    from swarm_engine.synthesis.admission import SmokeTest
    s_args, s_expect = smoke_cases[0]
    goal = (goal_text or f"executable grounding of relational predicate "
            f"'{predicate}'")

    def _epistemic_standing(p: str, sid: Optional[str]) -> bool:
        """Live query against the governing lexicon using the single
        validity rule shared with sync_epistemic_revocation(): the law
        stands iff the lexicon still carries a LEARNED entry for its
        predicate governed by the SAME semantic id. Lets admission's
        epistemic-revocation guard allow audited vindication recovery
        when a contradiction was retired; never a precomputed answer."""
        try:
            from swarm_engine.cognition.revocation import _assess_entry
            return _assess_entry(lexicon.get(p), sid) is None
        except Exception:
            return False

    verdict = engine.admission.admit(
        goal, plan, smoke=SmokeTest(args=dict(s_args), expect=s_expect),
        name=plan["name"], epistemic_standing=_epistemic_standing)
    if not verdict.ok:
        return {"ok": False, "predicate": predicate,
                "reason": f"admission refused at stage={verdict.stage}: "
                          f"{verdict.reasons[:2]}"}
    engine.capabilities.bind_goal(goal, verdict.capability_id)
    rep = {"ok": True, "predicate": predicate,
           "capability_id": verdict.capability_id,
           "primitive_name": f"acquired.{verdict.capability_id}",
           "verdict": verdict.verdict,
           "reused": verdict.verdict == "reused",
           "n_smoke_cases": len(smoke_cases),
           "goal": goal}
    # Route the verified learned law to reification (fail-closed; never
    # breaks the export that already succeeded).
    rep["reification"] = _notify_reifier(engine, entry, predicate, goal)
    # Provenance for the evidence-driven revocation lifecycle: this
    # executable was admitted FROM the lexicon entry's currently governed
    # semantic capability. If later evidence contradicts that entry,
    # sync_epistemic_revocation() derives the invalidation from this row.
    try:
        from swarm_engine.cognition.revocation import (
            record_grounding_provenance)
        record_grounding_provenance(
            engine.capabilities, verdict.capability_id, predicate,
            entry.semantic_id, getattr(lexicon, "db_path", None))
    except Exception:
        pass
    return rep


def _capability_name(predicate: str,
                     slot_map: Optional[Dict[int, int]]) -> str:
    # Data-derived name (the predicate token comes from the caller, never
    # from a literal in this module); distinct slot bindings are distinct
    # input schemas, hence distinct capability names.
    base = f"grounding:{predicate}"
    if slot_map:
        base += "[s%s,s%s]" % (slot_map.get(0), slot_map.get(1))
    return base


def export_compound_grounding_capabilities(compound_objective: Any,
                                          lexicon: Any, engine: Any
                                          ) -> Dict[str, Any]:
    """Export one reusable engine capability per distinct clause wiring in
    a compound objective. The wirings (predicate, agent_slot,
    patient_slot) come from the compound's own structural clause
    signature; the role->slot renaming is alpha-renaming driven by that
    structure. Clauses whose predicate has no usable learned entry are
    reported as skipped (fail closed) -- the caller decides whether the
    compound can proceed without them.
    """
    wirings = list(getattr(compound_objective, "wirings", None) or [])
    seen = set()
    exported: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for w in wirings:
        key = (w.predicate, w.agent_slot, w.patient_slot)
        if key in seen:
            continue
        seen.add(key)
        slot_map = {0: w.agent_slot, 1: w.patient_slot}
        goal = (f"{getattr(compound_objective, 'description', '')} "
                f"[grounding clause {w.predicate} s{w.agent_slot},"
                f"s{w.patient_slot}]")
        rep = export_grounding_capability(
            lexicon, engine, w.predicate, goal_text=goal, slot_map=slot_map)
        (exported if rep["ok"] else skipped).append(rep)
    return {"exported": exported, "skipped": skipped,
            "n_wirings": len(wirings), "n_distinct": len(seen)}
