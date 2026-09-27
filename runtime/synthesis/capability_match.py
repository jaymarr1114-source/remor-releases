"""
General capability compatibility matching (M+21).

Scores active capabilities against a new goal using:
  1. Behavioral fit when examples are provided (primary, keyword-free)
  2. Structural fit from plan shape + bound constants vs numbers in the goal

Does NOT use task-specific synonym tables or benchmark keywords.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple


# General English cardinal words → int (numeral parsing, not task synonyms)
_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}


def extract_numbers(text: str) -> List[float]:
    """Extract numeric constants from free text (digits + English cardinals)."""
    if not text:
        return []
    found: List[float] = []
    for m in re.finditer(r"(?<![A-Za-z])(\d+(?:\.\d+)?)(?![A-Za-z])", text):
        found.append(float(m.group(1)))
    lower = text.lower()
    for w, n in _NUM_WORDS.items():
        if re.search(rf"\b{w}\b", lower):
            found.append(float(n))
    return found


def _walk_partials(node: Any, out: List[Dict[str, Any]]) -> None:
    if isinstance(node, dict):
        if "$partial" in node:
            out.append(node["$partial"])
        for v in node.values():
            _walk_partials(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_partials(v, out)


def plan_bound_numbers(plan: Optional[Dict[str, Any]]) -> List[float]:
    if not plan:
        return []
    partials: List[Dict[str, Any]] = []
    _walk_partials(plan, partials)
    nums: List[float] = []
    for p in partials:
        bound = p.get("bound") or {}
        for v in bound.values():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                nums.append(float(v))
    return nums


def plan_partial_ops(plan: Optional[Dict[str, Any]]) -> List[str]:
    if not plan:
        return []
    partials: List[Dict[str, Any]] = []
    _walk_partials(plan, partials)
    return [str(p.get("op")) for p in partials if p.get("op")]


def plan_has_map(plan: Optional[Dict[str, Any]]) -> bool:
    if not plan:
        return False
    for step in plan.get("steps") or []:
        if isinstance(step, dict) and _bare_op(step.get("op")) == "map":
            return True
    return False


def _bare_op(op: Any, registry=None) -> str:
    """Canonical bare op name for shape/polarity matching.

    The registry registers every primitive under its bare name and resolves
    qualified aliases ("data.map" -> "map") to it; real admitted plans use
    qualified names (planner templates emit them). Shape and polarity
    tables are keyed on bare names, so qualified plan ops must be
    normalized before comparison -- otherwise "data.map" is invisible to
    the map-shape detector and "computation.multiply" is invisible to the
    multiply/divide polarity guard, even though they name the same
    primitives as the bare forms the tables were written for. Without a
    registry, strip the family prefix (best-effort, same result for every
    real qualified name the registry accepts).
    """
    if not isinstance(op, str):
        return str(op)
    if registry is not None:
        try:
            resolved = registry.resolve(op)
            if resolved:
                return resolved
        except Exception:
            pass
    return op.split(".")[-1]


@dataclass
class MatchScore:
    capability_id: str
    score: float
    reasons: List[str]
    behavioral: Optional[float] = None
    structural: Optional[float] = None


# ---------------------------------------------------------------------------
# Acquired-code candidates (Boundary 5 integration gap B).
# ---------------------------------------------------------------------------
# Code-acquired capabilities -- e.g. recursively-acquired decomposition
# children -- are persisted as source + entrypoint in the `acquired_code`
# table, not as plans in `plan_capabilities`, so the plan-native candidate
# scan never saw them: a reworded goal for already-acquired behavior was
# invisible at gap-analysis time even though the evidence to recognise it
# was in hand. The wrapper below makes one such entry behaviorally
# scorable by the SAME generic scorer as a stored plan: a single-step plan
# that calls the entry's registered primitive on its recorded input
# names. The match stays purely behavioral -- the score comes only from
# executing the candidate on the query examples, never from the entry's
# name or stored description. Entries whose primitive is not currently
# registered, or whose inputs are unknown, are refused at construction:
# they cannot be executed, so they cannot behaviorally match (fail
# closed). The liveness gate is about executability, not identity: no
# name is ever compared against the goal here.


@dataclass
class AcquiredCodeCandidate:
    """Behaviorally-testable view over one `acquired_code` entry."""
    capability_id: str
    name: str
    plan: Dict[str, Any]
    goal: str = ""
    status: str = "active"
    via: str = "acquired_code"  # provenance marker for gap evidence


def acquired_code_candidates(
        entries: Optional[Sequence[Dict[str, Any]]],
        registry=None) -> List[AcquiredCodeCandidate]:
    """Wrap acquired_code entries as behaviorally-scorable candidates."""
    out: List[AcquiredCodeCandidate] = []
    for e in entries or []:
        e = e or {}
        name = e.get("name")
        cap_id = e.get("capability_id")
        if not name or not cap_id:
            continue
        if registry is not None:
            try:
                if registry.get(name) is None:
                    continue  # not executable in this process: cannot match
            except Exception:
                continue
        inputs = ((e.get("spec") or {}).get("inputs")
                  or (e.get("spec") or {}).get("input_names")) or []
        inputs = [i for i in inputs if isinstance(i, str)]
        if not inputs:
            continue  # unknown inputs: cannot call it, fail closed
        plan = {
            "name": name,
            "params": {i: "any" for i in inputs},
            "steps": [{"id": "call", "op": name,
                       "args": {i: {"$param": i} for i in inputs}}],
            "output": {"$step": "call"},
        }
        out.append(AcquiredCodeCandidate(
            capability_id=cap_id, name=name, plan=plan,
            goal=((e.get("spec") or {}).get("description") or "")))
    return out


def behavioral_score(record, examples: Sequence[Tuple[Dict[str, Any], Any]],
                     composer) -> Tuple[float, str]:
    if not examples or composer is None or not record.plan:
        return 0.0, "no examples or composer"
    ok = 0
    for args, expected in examples:
        try:
            out = composer.execute_sync(record.plan, dict(args))
            if out.get("success") and out.get("value") == expected:
                ok += 1
        except Exception:
            pass
    frac = ok / len(examples)
    return frac, f"behavioral {ok}/{len(examples)}"


def structural_score(record, goal: str, payload: Optional[Dict[str, Any]]) -> Tuple[float, str]:
    """Score without task keywords: constants + plan shape + payload keys."""
    reasons = []
    score = 0.0
    goal_nums = set(extract_numbers(goal))
    bound_nums = set(plan_bound_numbers(record.plan))
    if goal_nums and bound_nums:
        inter = goal_nums & bound_nums
        if inter:
            score += 0.5
            reasons.append(f"bound constants match goal numbers {sorted(inter)}")
        else:
            # explicit constant conflict
            reasons.append(f"constant mismatch goal={sorted(goal_nums)} bound={sorted(bound_nums)}")
            return 0.0, "; ".join(reasons)
    elif bound_nums and not goal_nums:
        reasons.append("capability has bounds but goal has no numbers")
        return 0.0, "; ".join(reasons)

    if plan_has_map(record.plan):
        # list-valued payload suggests element-wise applicability
        if payload:
            listish = any(isinstance(v, (list, tuple)) for v in payload.values())
            if listish:
                score += 0.3
                reasons.append("map plan + list payload")
            else:
                score += 0.05
                reasons.append("map plan without list payload")
        else:
            score += 0.15
            reasons.append("map plan structure")

    # param key overlap with payload
    params = set((record.plan or {}).get("params") or {})
    if payload and params:
        overlap = params & set(payload.keys())
        if overlap:
            score += 0.2
            reasons.append(f"param overlap {sorted(overlap)}")
        else:
            score *= 0.3
            reasons.append("param keys disagree with payload")

    return min(score, 1.0), "; ".join(reasons) or "no structural signal"




def admitted_semantic_constraints(goal: str, semantic_store) -> list:
    """Load admitted SemanticCapability objects whose description_key matches goal.

    Exact key match only — no synonym expansion.
    """
    if semantic_store is None or not goal:
        return []
    try:
        from swarm_engine.acquisition.semantic_capability import description_key
        key = description_key(goal)
        return list(semantic_store.find_by_key(key, state="admitted") or [])
    except Exception:
        return []


def apply_semantic_constraint(score: float, reasons: list, plan, interp: dict) -> tuple:
    """Adjust structural score using an admitted interpretation constraint.

    Supports multiplicative family with bound k against plan $partial bounds.
    Returns (new_score, reasons).
    """
    if not interp:
        return score, reasons
    family = interp.get("family")
    if family == "multiplicative":
        k = interp.get("k")
        bounds = plan_bound_numbers(plan)
        if k is None:
            return score, reasons
        if not bounds:
            reasons.append("semantic requires multiplicative k but plan has no bounds")
            return 0.0, reasons
        if float(k) in bounds or any(abs(float(k) - b) < 1e-9 for b in bounds):
            reasons.append(f"semantic constraint k={k} matches plan bounds")
            return max(score, 0.85), reasons
        reasons.append(f"semantic constraint k={k} conflicts with plan bounds {bounds}")
        return 0.0, reasons
    if family == "additive":
        b = interp.get("b")
        # additive partials are rarer; require explicit bound match if present
        bounds = plan_bound_numbers(plan)
        if b is not None and bounds and not any(abs(float(b) - x) < 1e-9 for x in bounds):
            reasons.append(f"semantic additive b={b} conflicts bounds {bounds}")
            return 0.0, reasons
    return score, reasons

def rank_compatible(
    records: Sequence[Any],
    goal: str,
    *,
    examples: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None,
    payload: Optional[Dict[str, Any]] = None,
    composer=None,
    registry=None,
    semantic_store=None,
    min_score: float = 0.55,
    acquired_candidates: Optional[Sequence[Any]] = None,
) -> List[MatchScore]:
    """Rank capabilities for a goal. Prefer behavioral evidence when present.

    `acquired_candidates` are behaviorally-scorable wrappers over
    `acquired_code` entries (see acquired_code_candidates()): they are
    scored by the same behavioral/structural rules as plan records, so a
    code-acquired capability that genuinely implements the queried
    behavior is found, and one that does not scores 0 like any other
    non-match.
    """
    if registry is None and composer is not None:
        registry = getattr(composer, "reg", None) or getattr(composer, "registry", None)
    sem_caps = admitted_semantic_constraints(goal, semantic_store)
    scored: List[MatchScore] = []
    for rec in list(records) + list(acquired_candidates or []):
        if getattr(rec, "status", None) not in (None, "active"):
            if getattr(rec, "status", "active") != "active":
                continue
        reasons: List[str] = []
        b_score = None
        s_score = None
        total = 0.0
        if examples and composer is not None:
            b_score, b_reason = behavioral_score(rec, examples, composer)
            reasons.append(b_reason)
            if b_score >= 1.0:
                total = 1.0
            elif b_score > 0:
                total = 0.4 + 0.6 * b_score
            else:
                continue
        else:
            s_score, s_reason = structural_score(rec, goal, payload)
            reasons.append(s_reason)
            total = s_score
            # M+22: polarity / family conflict without query examples
            if total >= min_score and registry is not None:
                conflict, creason = polarity_conflict(
                    goal, rec.plan, registry, composer=composer)
                if conflict:
                    reasons.append(f"polarity_conflict: {creason}")
                    total = 0.0
            # M+26: consume admitted SemanticCapability constraints
            if sem_caps and total > 0:
                for sc in sem_caps:
                    total, reasons = apply_semantic_constraint(
                        total, reasons, rec.plan, sc.interpretation or {})
                    if total <= 0:
                        break
        if total >= min_score:
            via = getattr(rec, "via", None)
            if via:
                reasons.append(f"matched via {via} (behavioral execution, "
                               f"not name lookup)")
            scored.append(MatchScore(
                capability_id=rec.capability_id,
                score=total,
                reasons=reasons,
                behavioral=b_score,
                structural=s_score,
            ))
    scored.sort(key=lambda m: -m.score)
    return scored


# ---------------------------------------------------------------------------
# M+22: operation-family / polarity from capability behavior + registry names
# ---------------------------------------------------------------------------
# Inverse pairs are relationships among *primitive identities* in the registry,
# not goal-language synonym tables. Goal tokens are matched only against actual
# registered primitive names (dynamic lexicon of the system).

_PRIMITIVE_INVERSES = {
    frozenset({"multiply", "divide"}),
    frozenset({"add", "subtract"}),
    frozenset({"power", "sqrt"}),  # weak but directional
}


def _inverse_name(op: str) -> set:
    out = set()
    for pair in _PRIMITIVE_INVERSES:
        if op in pair:
            out |= set(pair) - {op}
    return out


def infer_transform_class(plan, composer, probes=None):
    """Derive multiplicative vs additive vs unknown from plan behavior on probes.

    Uses only numeric I/O relationships — no goal text.
    """
    if composer is None or not plan:
        return "unknown", {}
    probes = probes or [[1, 2, 3], [2, 4], [3, 6]]
    pairs = []  # (x, y) element-wise
    for xs in probes:
        try:
            out = composer.execute_sync(plan, {"values": list(xs)})
            if not out.get("success"):
                continue
            ys = out.get("value")
            if not isinstance(ys, (list, tuple)) or len(ys) != len(xs):
                continue
            for x, y in zip(xs, ys):
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    pairs.append((float(x), float(y)))
        except Exception:
            continue
    if len(pairs) < 2:
        return "unknown", {"pairs": len(pairs)}
    # Fit y ≈ k*x  and  y ≈ x+b
    ks, bs = [], []
    for x, y in pairs:
        if abs(x) > 1e-12:
            ks.append(y / x)
        bs.append(y - x)
    def _stable(vals, tol=1e-6):
        if not vals:
            return None
        m = sum(vals) / len(vals)
        return m if all(abs(v - m) <= tol * max(1.0, abs(m)) for v in vals) else None
    k = _stable(ks)
    b = _stable(bs)
    meta = {"k": k, "b": b, "n": len(pairs)}
    if k is not None and abs(k - 1.0) > 1e-9:
        return "multiplicative", meta
    if b is not None and abs(b) > 1e-9 and (k is None or abs(k - 1.0) <= 1e-9):
        return "additive", meta
    return "unknown", meta


def goal_registry_ops(goal: str, registry) -> set:
    """Primitive names that appear as whole-word tokens in the goal.

    Lexicon = registry.names() only — no external synonym table.
    """
    if not goal or registry is None:
        return set()
    try:
        names = set(registry.names())
    except Exception:
        names = set()
    import re
    tokens = set(re.findall(r"[a-z_][a-z0-9_]*", goal.lower()))
    return tokens & names


def polarity_conflict(goal: str, plan, registry, composer=None) -> tuple:
    """Return (conflicts: bool, reason: str).

    Conflict when the goal explicitly names a registered primitive that is the
    inverse of the capability's partial op OR inverse of the inferred transform
    family.
    """
    named = goal_registry_ops(goal, registry)
    if not named:
        return False, "no registry op named in goal"
    plan_ops = set(plan_partial_ops(plan)) | set(
        s.get("op") for s in (plan or {}).get("steps") or [] if isinstance(s, dict)
    )
    # Normalize to canonical bare names: plans admitted from real planner
    # templates use qualified ops ("computation.multiply") while the
    # inverse tables below are keyed on bare names ("multiply"); without
    # normalization the guard is blind to qualified-name plans (a
    # "divide by 2" goal would not conflict with a "multiply by 2" plan).
    bare_ops = {_bare_op(o, registry) for o in plan_ops}
    # Direct inverse of plan ops
    for op in bare_ops:
        inv = _inverse_name(str(op))
        hit = named & inv
        if hit:
            return True, f"goal names inverse op {sorted(hit)} of plan op {op}"
    # Family-level: multiplicative capability vs goal naming divide
    tclass, meta = infer_transform_class(plan, composer)
    if tclass == "multiplicative":
        # Goal names an op from a different algebraic family
        if named & {"divide", "add", "subtract"}:
            return True, (
                f"multiplicative transform (k={meta.get('k')}) incompatible with "
                f"goal-named ops {sorted(named & set(['divide','add','subtract']))}"
            )
    if tclass == "additive":
        if named & {"multiply", "divide"}:
            return True, (
                f"additive transform (b={meta.get('b')}) incompatible with "
                f"goal-named ops {sorted(named & set(['multiply','divide']))}"
            )
        if "subtract" in named and (meta.get("b") or 0) > 0:
            return True, f"additive +b vs goal naming subtract"
        if "add" in named and (meta.get("b") or 0) < 0:
            return True, f"additive -b vs goal naming add"
    # Goal names an op that is not the plan op and is inverse-related
    for gop in named:
        if gop in bare_ops:
            continue
        # if goal names multiply but plan is lcm — compatible family, not conflict
        inv = _inverse_name(gop)
        if inv & bare_ops:
            return True, f"goal op {gop} inverse-related to plan ops {sorted(bare_ops)}"
    return False, "no polarity conflict"




# ---------------------------------------------------------------------------
# Adaptation: bound-constant substitution with behavioral re-verification
# ---------------------------------------------------------------------------
# Retrieval replays admitted contracts: a capability whose bound constants
# do not coincide with the goal's numbers scores 0 structurally, and a
# capability that does not reproduce the caller's examples scores 0
# behaviorally. Neither path generalizes across the constant -- the
# capability-level analogue of the retention finding (retention replays
# exposure vocabulary; it does not generalize).
#
# adapt_compatible() is the first generalization mechanism: for a
# structural near-miss (exactly one distinct bound constant in the plan,
# exactly one distinct number in the goal, and they differ), it
# substitutes the goal's number for the plan's bound constant and
# behaviorally re-verifies the SUBSTITUTED plan against the caller's
# examples. The result is a PROPOSAL, not an admission:
#
#   * the adapted plan is never stored, registered, or executed under the
#     source capability's identity -- that would be the substitution
#     attack capability_store.py guards against (same id, different
#     bytes). The caller must run the proposal through
#     AdmissionController.admit(): new plan bytes get a new fingerprint,
#     new capability id, and the full type/effect/permission/smoke
#     pipeline.
#   * verification requires caller-supplied examples and demands 100%
#     reproduction -- no examples, no adaptation (fail closed).
#   * ambiguous substitutions (several distinct bounds, several distinct
#     goal numbers) are refused rather than guessed.
#
# Bounds of the mechanism (by construction, not by tuning): single
# bound-constant substitution only. Multi-constant plans, structural
# rewrites, and param-shape changes are out of scope and refused.


@dataclass
class AdaptationProposal:
    """A behaviorally-verified variant of an admitted capability.

    `plan` is the adapted plan bytes -- UNADMITTED. `derived_from` names
    the source capability for provenance. `substitution` maps the old
    bound value to the new one. `arg_alignment` records an example-arg ->
    plan-param renaming applied during verification (None when the
    caller's example args already matched the plan's params).
    """
    derived_from: str
    plan: Dict[str, Any]
    substitution: Dict[str, float]
    goal: str
    behavioral: float
    arg_alignment: Optional[Dict[str, str]] = None
    reasons: List[str] = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []


def _rewrite_partial_bounds(plan: Dict[str, Any], old: float,
                            new: float) -> Dict[str, Any]:
    """Deep-copy `plan`, replacing $partial bound values equal to `old`
    with `new`. Only bound values are touched -- plan structure, params,
    and step wiring are byte-identical otherwise."""
    import copy

    plan = copy.deepcopy(plan)

    def walk(node: Any):
        if isinstance(node, dict):
            if "$partial" in node and isinstance(node["$partial"], dict):
                bound = node["$partial"].get("bound")
                if isinstance(bound, dict):
                    for k, v in list(bound.items()):
                        if isinstance(v, (int, float)) and not isinstance(v, bool) \
                                and float(v) == old:
                            bound[k] = new
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    return plan


def _align_example_args(plan: Dict[str, Any],
                        examples: Sequence[Tuple[Dict[str, Any], Any]]
                        ) -> Tuple[List[Tuple[Dict[str, Any], Any]],
                                   Optional[Dict[str, str]]]:
    """Align caller example args to the plan's param names.

    The behavioral gate otherwise fails on a naming accident: examples
    keyed {"numbers": [...]} against a plan with param "values" are not a
    behavioral mismatch, they are the same single argument under a
    different name. Alignment applies ONLY in the unambiguous 1-param /
    1-arg case; anything else is returned unchanged (the caller must
    supply matching args, or the candidate simply does not verify).
    """
    params = list((plan or {}).get("params") or {})
    if len(params) != 1:
        return list(examples), None
    aligned = []
    mapping = None
    for args, expected in examples:
        if not isinstance(args, dict) or len(args) != 1:
            return list(examples), None
        (k,) = args.keys()
        if k == params[0]:
            aligned.append((args, expected))
            continue
        if mapping is None:
            mapping = {k: params[0]}
        elif mapping.get(k) != params[0]:
            return list(examples), None
        aligned.append(({params[0]: args[k]}, expected))
    if mapping is None:
        return list(examples), None
    return aligned, mapping


def adapt_compatible(
    records: Sequence[Any],
    goal: str,
    examples: Sequence[Tuple[Dict[str, Any], Any]],
    composer,
    *,
    registry=None,
) -> List[AdaptationProposal]:
    """Propose bound-constant adaptations of near-miss capabilities.

    For each active record whose plan carries exactly one distinct bound
    constant and whose goal carries exactly one distinct number (and the
    two differ), substitute and behaviorally re-verify the adapted plan
    on ALL caller examples. Only 1.0 reproduction yields a proposal.

    Fail-closed: no examples -> no proposals; ambiguous (multi-value)
    substitutions -> refused; partial reproduction -> refused; records
    that already reproduce the examples need no adaptation and are
    skipped. Returned proposals are UNADMITTED -- see the section
    header for the trust contract.
    """
    if not examples or composer is None:
        return []
    goal_nums = set(extract_numbers(goal))
    if len(goal_nums) != 1:
        return []  # no single target constant: substitution would be a guess
    (new_num,) = goal_nums
    proposals: List[AdaptationProposal] = []
    for rec in records:
        if getattr(rec, "status", None) not in (None, "active"):
            if getattr(rec, "status", "active") != "active":
                continue
        plan = getattr(rec, "plan", None)
        if not plan:
            continue
        bound = set(plan_bound_numbers(plan))
        if len(bound) != 1:
            continue  # zero or several distinct bounds: not adaptable
        (old_num,) = bound
        if old_num == new_num:
            continue  # not an adaptation -- rank_compatible's domain
        adapted = _rewrite_partial_bounds(plan, old_num, new_num)
        aligned_examples, mapping = _align_example_args(adapted, examples)
        probe = type("AdaptProbe", (), {
            "capability_id": getattr(rec, "capability_id", "?"),
            "plan": adapted, "status": "active"})()
        b_score, b_reason = behavioral_score(probe, aligned_examples, composer)
        if b_score < 1.0:
            continue  # the adapted plan does not reproduce the examples
        proposals.append(AdaptationProposal(
            derived_from=getattr(rec, "capability_id", "?"),
            plan=adapted,
            substitution={f"{old_num}": new_num},
            goal=goal,
            behavioral=b_score,
            arg_alignment=mapping,
            reasons=[
                f"bound-constant substitution {old_num} -> {new_num}",
                f"behavioral {b_reason} on adapted plan"
                + (f" (example args aligned {mapping})"
                   if mapping else ""),
            ],
        ))
    return proposals
