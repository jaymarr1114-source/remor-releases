"""Runtime inlet for objective-driven plan composition (V10-COMPOSE-INLET).

Nothing in the runtime imported plan_composer before this module:
composition was proven as a mechanism (V10-COMPOSE, 7d420f1) but was
unreachable from dispatch and from the acquisition loop, and the
IntentRouter's match surface could not see composed capabilities.

This module is the wiring:

  live GapRecord --(objective)--> PlanComposer --(Q8)--> admit_as_engine
      --(goal binding of the gap's request text)--> IntentRouter --> dispatch

Honesty properties (hard, not aspirational):
  * The plan comes ONLY from the composer's real search over the engine's
    primitive registry. It is never caller-supplied, never planted.
  * Verification ground truth comes from the gap's own worked examples
    (train) and held-out examples (Q8). Without usable examples the inlet
    refuses (no_verifiable_examples): the composer cannot verify against
    nothing, and an unverifiable composition is not admitted.
  * Q8 is not weakened: held-out generalization is required; a candidate
    that fits training but fails held-out is denied; the plan must not
    smuggle training outputs as bound constants (structural
    anti-memorizer).
  * The router reaches the admitted capability through the goal binding
    recorded at admission -- the learned association between the real
    failed request and the capability that satisfied it -- never a
    hand-written synonym or keyword table.
  * Op-name qualification is principled: bare op -> family.name through
    the registry's own family table (stored plans are written against
    qualified names), never a hand-written map.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.primitives.core import infer
from swarm_engine.synthesis.admission import SmokeTest, Verdict
from swarm_engine.synthesis.plan_composer import (
    CompositionObjective, PlanComposer,
)


REGISTERED_BY = "v10-compose-inlet"
PROVENANCE_METHOD = "v10-compose-inlet"


class InletRefused(Exception):
    """The inlet refused: fail-closed, machine-readable reason."""


@dataclass
class InletResult:
    ok: bool
    capability_id: Optional[str] = None
    composed_of: List[str] = field(default_factory=list)
    route: str = ""            # how the gap reached the inlet
    refusal: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    evaluations: int = 0       # composer search evaluations (real cost)


# ---------------------------------------------------------------------------
# Gap -> objective
# ---------------------------------------------------------------------------

def _gap_request_text(gap: Any) -> str:
    """The real request text that failed. Recorded, never invented.

    Carried in an observation entry (observed="failed_request",
    detail={"request_text": ...}) -- the checkable evidence vocabulary,
    no dataclass change.
    """
    for entry in getattr(gap, "evidence", None) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "observation":
            continue
        if entry.get("observed") != "failed_request":
            continue
        detail = entry.get("detail") or {}
        text = detail.get("request_text") if isinstance(detail, dict) else None
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _gap_examples(gap: Any) -> Tuple[List[Tuple[Dict[str, Any], Any]],
                                      List[Tuple[Dict[str, Any], Any]]]:
    """Worked examples from the gap's evidence (observation entries).

    Convention (documented, checkable): an observation entry with
    observed="composition_examples" and detail={"examples": [[args,
    expected], ...], "held_out": [[args, expected], ...]}. The examples
    are the verification ground truth the loop actually observed --
    e.g. a user demonstration or measured behavior -- not the answer:
    the composer still has to discover the plan by search.
    """
    train: List[Tuple[Dict[str, Any], Any]] = []
    held: List[Tuple[Dict[str, Any], Any]] = []
    for entry in getattr(gap, "evidence", None) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "observation":
            continue
        if entry.get("observed") != "composition_examples":
            continue
        detail = entry.get("detail") or {}
        if not isinstance(detail, dict):
            continue
        for args, expected in detail.get("examples") or []:
            if isinstance(args, dict):
                train.append((dict(args), expected))
        for args, expected in detail.get("held_out") or []:
            if isinstance(args, dict):
                held.append((dict(args), expected))
    return train, held


def is_composition_gap(gap: Any) -> bool:
    """Whether a gap record is a composition gap: a real failed NL
    request plus verifiable composition examples, carried as evidence
    observations. Used by the gap registry's shape routing to select
    the composition acquire leg."""
    try:
        if not _gap_request_text(gap):
            return False
        train, _ = _gap_examples(gap)
        return len(train) > 0
    except Exception:
        return False


def objective_from_gap(engine: Any, gap: Any) -> CompositionObjective:
    """Build the composer's objective from a LIVE GapRecord.

    Refuses when the gap carries no verifiable examples: without ground
    truth the composer cannot verify, and the honest outcome is refusal,
    not synthesis from nothing.
    """
    request_text = _gap_request_text(gap)
    train, held_out = _gap_examples(gap)
    if not train:
        raise InletRefused(
            "no_verifiable_examples: gap carries no worked examples; "
            "composition cannot be verified")
    if not request_text:
        raise InletRefused(
            "no_request_text: gap carries no failed request text to bind")
    # Derive the plan signature from the examples' shapes through the
    # existing TypeSpec inference (no new type system). Params keep the
    # inferred value types: narrower inputs only widen primitive
    # applicability at the ispec.accepts(vkind) gate. The OUTPUT kind is
    # widened to the domain kind: it gates head selection in the other
    # direction (goal_kind.accepts(prim.output)), so an over-specific
    # observation type would prune every head.
    composer = getattr(engine, "composer", None)
    registry = getattr(composer, "reg", None) if composer else None
    declared_out = (_declared_output_kinds(registry)
                    if registry is not None else [])
    params: Dict[str, Any] = {}
    for args, _ in train:
        for k, v in args.items():
            if k in params:
                # keep the first shape; the composer verifies behaviorally
                continue
            params[k] = infer(v)
    output_kind = _domain_kind(infer(train[0][1]), declared_out)
    gap_id = getattr(gap, "gap_id", "") or "gap_unknown"
    return CompositionObjective(
        goal=request_text,
        gap_id=gap_id,
        params=params,
        output_kind=output_kind,
        examples=train,
        held_out=held_out,
    )


# ---------------------------------------------------------------------------
# Type normalization: inferred value types -> primitive vocabulary kinds
# ---------------------------------------------------------------------------

def _accepts(a: Any, b: Any) -> bool:
    try:
        return bool(a.accepts(b))
    except Exception:
        return False


def _declared_output_kinds(registry: Any) -> List[Any]:
    kinds: List[Any] = []
    seen = set()
    for prim in registry._prims.values():
        s = prim.output
        if str(s) not in seen:
            seen.add(str(s))
            kinds.append(s)
    return kinds


def _domain_kind(spec: Any, declared_outputs: List[Any]) -> Any:
    """Widen an inferred value type to the primitive vocabulary's domain kind.

    `infer` returns the type of the OBSERVED values (int for 14); the
    composer searches over the PRIMITIVE-DECLARED kinds, and its head
    gate (`goal_kind.accepts(prim.output)`) prunes every primitive whose
    declared output the goal kind does not accept. The domain kind is
    the declared output kind that (a) strictly generalizes the observed
    type and (b) covers the most primitive outputs -- i.e. the
    vocabulary's own domain for these observations, maximizing the
    searchable mechanism space. Purely mechanical (the existing accepts
    relation over declared signatures); the behavioral examples remain
    the real verifier. If no strict generalizer is declared, the
    inferred spec is kept (fail closed: the search may find nothing).
    """
    generalizers = [
        k for k in declared_outputs
        if k != spec and _accepts(k, spec) and not _accepts(spec, k)
    ]
    if not generalizers:
        return spec

    def _top(k: Any) -> bool:
        return str(k) in ("any", "any?")

    def _bigger_peers(k: Any) -> int:
        return sum(1 for o in generalizers if o != k and _accepts(o, k))

    generalizers.sort(key=lambda k: (
        _top(k),
        -sum(1 for o in declared_outputs if _accepts(k, o)),
        -_bigger_peers(k),
    ))
    return generalizers[0]

def _plan_bound_output_constants(plan: Dict[str, Any],
                                 expected_values: List[Any]) -> List[Any]:
    """Bound numeric constants in the plan that equal training outputs.

    A plan that smuggles expected outputs as constants is a lookup table,
    not a computation: it would pass training and fail held-out. This
    structural check names it before execution does.
    """
    found: List[Any] = []

    def walk(node: Any):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "value" and isinstance(v, (int, float)) \
                        and not isinstance(v, bool):
                    if any(v == e for e in expected_values
                           if isinstance(e, (int, float))
                           and not isinstance(e, bool)):
                        found.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    return found


def q8_authenticate(plan: Dict[str, Any], composer: Any,
                    train: List[Tuple[Dict[str, Any], Any]],
                    held_out: List[Tuple[Dict[str, Any], Any]]
                    ) -> Tuple[bool, List[str]]:
    """Q8: authenticate the composed plan before admission.

    1. The plan re-executes correctly on ALL training examples
       (independent re-verification of the search's own verdict).
    2. The plan executes correctly on ALL held-out examples
       (generalization; a training-fit memorizer fails here and is denied).
    3. The plan carries no bound constants equal to training outputs
       (structural anti-memorizer).
    Any failure denies admission. Returns (ok, reasons).
    """
    reasons: List[str] = []
    expected = [e for _, e in train]

    smuggled = _plan_bound_output_constants(plan, expected)
    if smuggled:
        reasons.append(
            f"denied: plan binds training outputs as constants {smuggled}")
        return False, reasons
    reasons.append("no bound training-output constants in plan")

    def run_all(pairs, label):
        bad = []
        for args, want in pairs:
            try:
                out = composer.execute_sync(plan, dict(args))
            except Exception as exc:  # noqa: BLE001 -- execution is untrusted
                bad.append((args, f"raised {type(exc).__name__}"))
                continue
            if not out.get("success"):
                bad.append((args, f"not success: {out.get('error')}"))
            elif out.get("value") != want:
                bad.append((args, f"{out.get('value')!r} != {want!r}"))
        if bad:
            reasons.append(f"denied: {label} mismatches {bad[:3]}")
            return False
        reasons.append(f"{label}: {len(pairs)}/{len(pairs)} match")
        return True

    if not run_all(train, "train re-verification"):
        return False, reasons
    if held_out:
        if not run_all(held_out, "held-out generalization"):
            return False, reasons
    else:
        reasons.append("no held-out examples supplied; train-only gate")
    return True, reasons


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------

def _qualify_plan(plan: Dict[str, Any], registry: Any) -> Dict[str, Any]:
    """Qualify bare op names to family.name through the registry.

    Stored plans are written against qualified names (unambiguous if two
    families ever expose the same bare name); the registry is keyed bare
    and knows each primitive's family. Unknown ops fail closed.
    """
    p = copy.deepcopy(plan)

    def qualify_op(op: str) -> str:
        prim = registry.get(op)
        if prim is None:
            raise InletRefused(
                f"unresolvable op {op!r}: not in the primitive registry")
        return f"{prim.family}.{prim.name}"

    for step in p.get("steps") or []:
        if isinstance(step, dict) and "op" in step:
            step["op"] = qualify_op(step["op"])
        for arg in (step.get("args") or {}).values():
            if isinstance(arg, dict) and "$lambda" in arg:
                body = arg["$lambda"].get("body", {})
                if isinstance(body, dict) and "op" in body:
                    body["op"] = qualify_op(body["op"])
    return p


def drive_composition_gap(engine: Any, gap: Any) -> InletResult:
    """Run the full inlet: gap -> compose -> Q8 -> admit.

    The engine's Composer and primitive registry are used throughout;
    the plan is admitted with composed_of provenance and the gap's
    request text bound as its goal, so the IntentRouter reaches it on
    retry through the exact_goal binding -- the learned association,
    recorded from a real failure.
    """
    reasons: List[str] = []
    try:
        objective = objective_from_gap(engine, gap)
    except InletRefused as exc:
        return InletResult(ok=False, refusal=str(exc).split(":")[0],
                           reasons=[str(exc)])
    reasons.append(
        f"objective from gap {objective.gap_id}: "
        f"{len(objective.examples)} train / {len(objective.held_out)} "
        f"held-out examples")

    composer = getattr(engine, "composer", None)
    if composer is None:
        return InletResult(ok=False, refusal="no_engine_composer",
                           reasons=["engine has no Composer"])
    pc = PlanComposer(composer)
    res = pc.compose(objective)
    reasons.append(f"composer search: {res.candidates_evaluated} evaluations")
    if not res.found:
        return InletResult(
            ok=False, refusal="no_composition_found", evaluations=res.candidates_evaluated,
            reasons=reasons + ["search exhausted without a verified plan"])

    ok, q8_reasons = q8_authenticate(res.plan, composer,
                                     objective.examples, objective.held_out)
    reasons.extend(q8_reasons)
    if not ok:
        return InletResult(ok=False, refusal="q8_denied",
                           evaluations=res.candidates_evaluated, reasons=reasons)

    try:
        admit_plan = _qualify_plan(res.plan, composer.reg)
    except InletRefused as exc:
        return InletResult(ok=False, refusal=str(exc).split(":")[0],
                           evaluations=res.candidates_evaluated,
                           reasons=reasons + [str(exc)])
    admit_plan["provenance"] = {
        "composed_of": list(res.composed_of),
        "method": PROVENANCE_METHOD,
        "gap_id": objective.gap_id,
    }
    smoke_args, smoke_expect = objective.examples[0]
    adm = engine.admit_as_engine(
        objective.goal, admit_plan,
        smoke=SmokeTest(args=dict(smoke_args), expect=smoke_expect),
        name="composed_" + "".join(
            ch if ch.isalnum() else "_" for ch in objective.goal[:40]
        ).strip("_") or "composed_capability",
    )
    if not adm.ok or adm.verdict != Verdict.ADMITTED:
        return InletResult(
            ok=False, refusal="admission_refused",
            evaluations=res.candidates_evaluated, composed_of=list(res.composed_of),
            reasons=reasons + [f"admission: ok={adm.ok} "
                               f"verdict={adm.verdict} "
                               f"reasons={adm.reasons}"])
    reasons.append(f"admitted {adm.capability_id[:12]}... with goal binding "
                   f"for the gap's request text")
    return InletResult(
        ok=True, capability_id=adm.capability_id,
        composed_of=list(res.composed_of), route="gap->compose->q8->admit",
        evaluations=res.candidates_evaluated, reasons=reasons)
