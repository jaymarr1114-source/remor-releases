"""
swarm_engine/acquisition/strategies.py

Acquisition strategy selection, and sources that generate candidates instead
of retrieving them.

The pipeline previously drew candidates from a catalogue someone else filled
in. That tests the *evaluation* half of acquisition honestly, but it assumes
away the interesting half: a benchmark that hands SWarm the answer is not
measuring acquisition, it is measuring admission. For the loop to mean
anything, SWarm has to produce the candidate itself.

So the engine first decides *how* to acquire, then uses a source that can
actually build the thing:

  COMPOSE   - the capability is reachable by composing primitives it already
              has. Cheapest, safest, and always tried first: nothing is
              acquired that could have been assembled.
  GENERATE  - construct source code from a specification.
  RETRIEVE  - fetch an implementation from an allowed external source.
  DELEGATE  - hand the sub-problem to another model or agent.
  EXPERIMENT- derive the behaviour from worked examples.

Strategy selection is explicit and reported, because "how did you get this
capability?" is a provenance question, and an engine that cannot answer it
cannot justify trusting the result.

An honest boundary: the generator here is a *program synthesizer over a
specification*, not a language model. It builds code from examples, declared
shape, and a library of parameterised skeletons. It genuinely produces
capabilities the engine did not previously have, and it genuinely fails on
specifications outside its reach — it does not pretend to open-ended code
generation, and `GenerationReport.method` always says which mechanism
produced a candidate so the result is never mistaken for more than it is.
"""
from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from swarm_engine.acquisition.pipeline import (
    Candidate, CandidateSource, CapabilityRequirement,
)

try:
    import numpy as _np
except Exception:  # pragma: no cover - numpy is a hard venv dependency
    _np = None


class Strategy(Enum):
    COMPOSE = "compose"
    GENERATE = "generate"
    RETRIEVE = "retrieve"
    DELEGATE = "delegate"
    EXPERIMENT = "experiment"
    # M+8: target-driven route for structural representation/constructibility gaps
    STRUCTURAL = "structural_representation"
    # M+24: novel operation description without grounded semantics
    SEMANTIC_INTERPRETATION = "semantic_operation_interpretation"


@dataclass
class StrategyChoice:
    strategy: Strategy
    rationale: str
    confidence: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"strategy": self.strategy.value, "rationale": self.rationale,
                "confidence": round(self.confidence, 2)}


@dataclass
class CapabilitySpec:
    """A formal statement of what is wanted, independent of how it is built."""
    name: str
    description: str = ""
    examples: List[Tuple[Dict[str, Any], Any]] = field(default_factory=list)
    input_names: List[str] = field(default_factory=list)
    output_kind: str = ""
    required_effects: List[str] = field(default_factory=list)
    invariants: List[str] = field(default_factory=list)
    # M+7: preserve structural acquisition target (interpretation only — not synthesis)
    constraints: Dict[str, Any] = field(default_factory=dict)
    acquisition_target: Optional[Dict[str, Any]] = None
    # O19: provenance id of the driver example batch these examples came
    # from (governance/examples_provenance.py), when the requirement
    # carried one.
    examples_batch_id: Optional[str] = None

    @classmethod
    def from_requirement(cls, requirement: CapabilityRequirement,
                         examples: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None
                         ) -> "CapabilitySpec":
        examples = list(examples or [])
        input_names = sorted(examples[0][0].keys()) if examples else []
        output_kind = type(examples[0][1]).__name__ if examples else ""
        constraints = dict(getattr(requirement, "constraints", None) or {})
        target = interpret_acquisition_target(requirement, examples)
        return cls(name=requirement.name, description=requirement.description,
                   examples=examples, input_names=input_names,
                   output_kind=output_kind,
                   required_effects=list(requirement.required_effects),
                   constraints=constraints,
                   acquisition_target=target,
                   examples_batch_id=getattr(
                       requirement, "examples_batch_id", None))

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "examples": len(self.examples), "inputs": self.input_names,
                "output_kind": self.output_kind,
                "required_effects": self.required_effects,
                "constraints": self.constraints,
                "acquisition_target": self.acquisition_target}


def interpret_acquisition_target(
        requirement: Any,
        examples: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None
        ) -> Optional[Dict[str, Any]]:
    """Translate gap evidence into an explicit acquisition target.

    Structural: unconstructible CALLABLE slots → structural_representation_capability.
    Semantic (M+24): unclassified goal with numeric structure but no examples and
    no registry-grounded operation identity → semantic_operation_interpretation.
    Does NOT invent the meaning of the description.
    """
    constraints = dict(getattr(requirement, "constraints", None) or {})
    unsat = list(constraints.get("unsatisfiable_args") or [])
    name = getattr(requirement, "name", "") or ""
    description = getattr(requirement, "description", "") or ""
    # --- M+24 semantic interpretation target (before early structural None) ---
    if not examples:
        import re
        has_num = bool(re.search(
            r"\d|\b(seven|six|five|four|three|two|one|eight|nine|ten)\b",
            description.lower()))
        looks_unclassified = (
            name == "unclassified"
            or "unclassified" in name
            or "no template" in description.lower()
            or "no plan" in description.lower()
            or "no matching" in description.lower()
            or "no constructible" in description.lower()
        )
        if has_num and looks_unclassified:
            return {
                "class": "semantic_operation_interpretation",
                "required_kind": "SEMANTIC_MAPPING",
                "required_for": "goal_operation_family",
                "source_goal": description,
                "evidence_required": [
                    "behavioral_examples",
                    "or_registry_grounded_op_identity",
                    "or_validated_semantic_capability",
                ],
                "note": (
                    "Goal description is not grounded in registered primitive "
                    "identity and no behavioral examples were supplied; "
                    "operation/family semantics cannot be determined from "
                    "structure alone."
                ),
                "constructible_by_current_search": False,
            }
    if name != "structural_unconstructible_argument" and not unsat:
        return None
    callable_slots = [
        u for u in unsat
        if "callable" in str(u.get("required_type", "")).lower()
    ]
    if not callable_slots and name != "structural_unconstructible_argument":
        return None
    primary = next(
        (u for u in callable_slots if u.get("op") in ("map", "data.map")),
        callable_slots[0] if callable_slots else None,
    )
    return {
        "class": "structural_representation_capability",
        "required_kind": "CALLABLE",
        "required_for": (
            f"{primary.get('op')}.{primary.get('arg_name')}" if primary else None
        ),
        "required_type": (primary or {}).get("required_type", "callable"),
        "constructible_by_current_search": False,
        "type_exists": True,
        "unsatisfiable_args": callable_slots[:10] or unsat[:10],
        "behavioral_examples": len(list(examples or [])),
        "source": "observed_structural_exclusion",
        "requirement_name": name,
    }


class StrategySelector:
    """Decides how a missing capability should be acquired.

    Ordering is a cost and risk ordering, not a preference: composing what the
    engine already has introduces no new code and no new trust question, so it
    is always considered first. Retrieval — running someone else's code — is
    considered last among the mechanisms that can actually produce something,
    because it is the only one that introduces an outside party.
    """

    def select(self, spec: CapabilitySpec, registry=None,
               network_available: bool = False,
               delegate_available: bool = False) -> List[StrategyChoice]:
        choices: List[StrategyChoice] = []

        # Target-driven selection (M+8): structural representation targets
        # outrank generic compose/generate so strategy is caused by
        # acquisition_target.class, not task text.
        target = getattr(spec, "acquisition_target", None) or {}
        if target.get("class") == "structural_representation_capability":
            choices.append(StrategyChoice(
                Strategy.STRUCTURAL,
                "acquisition_target.class=structural_representation_capability "
                f"requires constructible {target.get('required_kind')} for "
                f"{target.get('required_for')}; type exists but "
                "constructible_by_current_search=false",
                0.95))
        if target.get("class") == "semantic_operation_interpretation":
            choices.append(StrategyChoice(
                Strategy.SEMANTIC_INTERPRETATION,
                "acquisition_target.class=semantic_operation_interpretation: "
                "operation/family semantics are not grounded; requires "
                "behavioral evidence, registry-grounded identity, or a "
                "validated semantic capability — no keyword/synonym strategy",
                0.9))

        if registry is not None and self._composable(spec, registry):
            choices.append(StrategyChoice(
                Strategy.COMPOSE,
                "the vocabulary already contains primitives whose names and "
                "shapes match this requirement", 0.8))

        if spec.examples:
            choices.append(StrategyChoice(
                Strategy.GENERATE,
                f"{len(spec.examples)} worked example(s) constrain the "
                f"behaviour well enough to synthesize an implementation", 0.7))
            choices.append(StrategyChoice(
                Strategy.EXPERIMENT,
                "behaviour can be induced from the examples directly", 0.4))
        else:
            choices.append(StrategyChoice(
                Strategy.GENERATE,
                "no examples supplied; only skeleton synthesis is possible "
                "and it cannot be validated against expected behaviour", 0.2))

        if network_available:
            choices.append(StrategyChoice(
                Strategy.RETRIEVE,
                "an external source is reachable and permitted", 0.5))
        if delegate_available:
            choices.append(StrategyChoice(
                Strategy.DELEGATE,
                "another agent or model can supply an implementation", 0.4))

        choices.sort(key=lambda c: -c.confidence)
        return choices

    def _composable(self, spec: CapabilitySpec, registry) -> bool:
        # Structural decomposition parents are composable exactly when every
        # named child is already a registered primitive: the pair-assembly
        # plan calls the children by name, so "all children present" is the
        # necessary and sufficient condition. This is decided from the
        # requirement's own structural metadata, never from goal vocabulary.
        # Gap-A: resolve through the parent's `resolved_primitives` map --
        # a child satisfied by behavioral discovery executes under its
        # discovered primitive name, not the decomposition's node name.
        asm = dict(getattr(spec, "constraints", None) or {}).get(
            "output_decomposition")
        if isinstance(asm, dict):
            children = asm.get("children") or []
            resolved = asm.get("resolved_primitives") or {}
            if children and all(
                    registry.get(resolved.get(c, c)) is not None
                    for c in children):
                return True
        # Behavioral decomposition parents (P8) are composable exactly when
        # every named child is already a registered primitive: the
        # behavioral assembly calls the children by name, so "all children
        # present" is the necessary and sufficient condition. Decided from
        # the requirement's own behavioral metadata, never from goal
        # vocabulary. Resolve through the parent's `resolved_primitives`
        # map -- a child satisfied by behavioral discovery executes under
        # its discovered primitive name, not the decomposition's node name.
        bdecomp = dict(getattr(spec, "constraints", None) or {}).get(
            "behavioral_decomposition")
        if isinstance(bdecomp, dict):
            children = bdecomp.get("children") or bdecomp.get("child_refs") or []
            resolved = bdecomp.get("resolved_primitives") or {}
            if children and all(
                    registry.get(resolved.get(c, c)) is not None
                    for c in children):
                return True
        words = {w for w in re.split(r"[^a-z0-9]+", spec.name.lower()) if len(w) > 2}
        words |= {w for w in re.split(r"[^a-z0-9]+", spec.description.lower())
                  if len(w) > 3}
        return any(registry.search(word) for word in words)


# ---------------------------------------------------------------------------
# GENERATING SOURCES
# ---------------------------------------------------------------------------

@dataclass
class GenerationReport:
    produced: int = 0
    method: str = ""
    attempted: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# Parameterised skeletons. Each is a *hypothesis* about what the capability
# might be; the verifier decides which (if any) actually satisfies the
# examples. Generating many and testing them is deliberate: a synthesizer that
# emits one guess and asserts it is right is the circular-validation failure
# this whole architecture exists to avoid.
_SKELETONS: List[Tuple[str, str]] = [
    ("reverse_sequence", "def capability({args}):\n    return {a0}[::-1]\n"),
    ("identity", "def capability({args}):\n    return {a0}\n"),
    ("upper", "def capability({args}):\n    return {a0}.upper()\n"),
    ("lower", "def capability({args}):\n    return {a0}.lower()\n"),
    ("strip", "def capability({args}):\n    return {a0}.strip()\n"),
    ("length", "def capability({args}):\n    return len({a0})\n"),
    ("sum_sequence", "def capability({args}):\n    return sum({a0})\n"),
    ("max_sequence", "def capability({args}):\n    return max({a0})\n"),
    ("min_sequence", "def capability({args}):\n    return min({a0})\n"),
    ("sorted_sequence", "def capability({args}):\n    return sorted({a0})\n"),
    ("unique_sequence",
     "def capability({args}):\n"
     "    seen = []\n"
     "    for item in {a0}:\n"
     "        if item not in seen:\n"
     "            seen.append(item)\n"
     "    return seen\n"),
    ("mean_sequence",
     "def capability({args}):\n"
     "    values = list({a0})\n"
     "    if not values:\n"
     "        raise ValueError('empty sequence')\n"
     "    return sum(values) / len(values)\n"),
    ("word_count",
     "def capability({args}):\n    return len({a0}.split())\n"),
    ("title_case", "def capability({args}):\n    return {a0}.title()\n"),
    ("count_chars",
     "def capability({args}):\n    return len({a0})\n"),
    ("join_words",
     "def capability({args}):\n    return ' '.join({a0})\n"),
    ("split_words",
     "def capability({args}):\n    return {a0}.split()\n"),
    ("abs_value", "def capability({args}):\n    return abs({a0})\n"),
    ("square", "def capability({args}):\n    return {a0} ** 2\n"),
    ("double", "def capability({args}):\n    return {a0} * 2\n"),
    ("negate", "def capability({args}):\n    return -{a0}\n"),
    ("add_two", "def capability({args}):\n    return {a0} + {a1}\n"),
    ("multiply_two", "def capability({args}):\n    return {a0} * {a1}\n"),
    ("subtract_two", "def capability({args}):\n    return {a0} - {a1}\n"),
    ("concat_two", "def capability({args}):\n    return {a0} + {a1}\n"),
    # Generic binary relations.  These candidate shapes introduce no task
    # identity or natural-language interpretation: behavioural examples and
    # independent validation decide whether any relation is correct.
    ("equals_two", "def capability({args}):\n    return {a0} == {a1}\n"),
    ("not_equals_two", "def capability({args}):\n    return {a0} != {a1}\n"),
    ("less_than_two", "def capability({args}):\n    return {a0} < {a1}\n"),
    ("less_or_equal_two", "def capability({args}):\n    return {a0} <= {a1}\n"),
    ("greater_than_two", "def capability({args}):\n    return {a0} > {a1}\n"),
    ("greater_or_equal_two", "def capability({args}):\n    return {a0} >= {a1}\n"),
    ("left_in_right", "def capability({args}):\n    return {a0} in {a1}\n"),
    ("right_in_left", "def capability({args}):\n    return {a1} in {a0}\n"),
]

# Generic result-kind tags, not task-specific: what TYPE OF VALUE each
# skeleton's own result is, independent of what it's called or what task
# it happens to be useful for. Used only to prune type-incompatible
# compositions (e.g. never try .upper() on a number's result) — this is
# the same kind of check the composer/type-system already makes
# elsewhere in this codebase, applied here so the search doesn't waste
# budget on candidates that could never type-check.
_NUMERIC_SKELETONS = {
    "abs_value", "square", "double", "negate", "add_two", "multiply_two",
    "subtract_two", "length", "sum_sequence", "max_sequence", "min_sequence",
    "mean_sequence", "word_count", "count_chars",
}
_SEQUENCE_SKELETONS = {
    "reverse_sequence", "identity", "upper", "lower", "strip",
    "sorted_sequence", "unique_sequence", "title_case", "join_words",
    "split_words", "concat_two",
}


# Two-stage compositions, so the synthesizer can reach behaviours no single
# skeleton covers -- reverse-then-upper, unique-then-sorted, and so on.
_STAGES: List[Tuple[str, str]] = [
    ("reverse", "{v}[::-1]"),
    ("upper", "{v}.upper()"),
    ("lower", "{v}.lower()"),
    ("strip", "{v}.strip()"),
    ("sorted", "sorted({v})"),
    ("list", "list({v})"),
    ("split", "{v}.split()"),
    ("join", "' '.join({v})"),
    ("len", "len({v})"),
    ("sum", "sum({v})"),
    ("set_list", "sorted(set({v}))"),
    ("title", "{v}.title()"),
]


def _skeleton_expression(template: str) -> Optional[str]:
    """Pull the bare return-expression out of a full skeleton template, so
    it can be reused as an intermediate value fed into a further stage,
    not just as a complete standalone function body."""
    marker = "    return "
    idx = template.find(marker)
    if idx < 0:
        return None
    return template[idx + len(marker):].rstrip("\n")


def _numeric_stages_from_skeletons() -> List[Tuple[str, str]]:
    """Every SINGLE-argument skeleton (numeric or string) doubles as a
    chainable stage automatically, derived from _SKELETONS itself rather
    than hand-duplicated into _STAGES separately. Found necessary
    directly: _STAGES was entirely string/sequence-oriented (reverse,
    upper, sorted, ...) and never included abs_value/square/double/negate
    at all, so a numeric skeleton's result could never be fed into a
    further numeric transform — the two lists had silently drifted apart.
    Deriving stages from the skeleton list is a single source of truth:
    any future single-arg skeleton is automatically chainable without a
    second, easily-forgotten registration."""
    out: List[Tuple[str, str]] = []
    for name, template in _SKELETONS:
        if "{a1}" in template:
            continue  # only single-argument skeletons compose this way
        expr = _skeleton_expression(template)
        if expr is None or "{a0}" not in expr:
            continue
        out.append((name, expr.replace("{a0}", "{v}")))
    return out


_ALL_STAGES: List[Tuple[str, str]] = _STAGES + _numeric_stages_from_skeletons()

# Single-value numeric transforms, derived directly from the single-argument
# numeric skeletons above (same operations, just written against a bound
# intermediate value {v} instead of the original argument {a0}) — not new
# operations, just the existing ones made reusable as an outer stage over
# ANY skeleton's result, not only sequence stages over args[0] alone.
_NUMERIC_STAGES: List[Tuple[str, str]] = [
    ("abs", "abs({v})"),
    ("square", "{v} ** 2"),
    ("double", "{v} * 2"),
    ("negate", "-{v}"),
]


class SynthesizingSource(CandidateSource):
    """Generates candidate implementations from a specification.

    This is the source that makes acquisition real rather than staged: nothing
    is looked up. Candidates are constructed, then handed to the same scanner,
    isolation and verification every other candidate faces — the synthesizer
    gets no privileges for having been written in-house, because "we wrote it"
    is not evidence of correctness.
    """
    requires_effect = None

    def __init__(self, spec_provider: Optional[Callable[[CapabilityRequirement],
                                                        CapabilitySpec]] = None,
                 max_candidates: int = 400,
                 producer_id: Optional[str] = None,
                 token: Optional[str] = None,
                 oracle_registry=None, engine_oracle=None):
        # Raised from 60: the general skeleton-then-transform composition
        # below (added to let ANY skeleton's result feed a type-compatible
        # outer transform, not only args[0] through sequence stages) adds
        # roughly 24 skeletons x ~12 outer transforms to the space. At the
        # old budget, the pre-existing args[0]-only stage permutations
        # alone (132 pairs) already exhausted it before the new loop ever
        # ran.
        self.spec_provider = spec_provider
        self.max_candidates = max_candidates
        self.last_report = GenerationReport()
        # O15: a caller-supplied spec provider determines the CapabilitySpec
        # (including examples) that generation searches against -- it is an
        # expectation-source oracle and, in bound mode, must be a registered
        # one. The engine-internal default path needs no binding.
        self.oracle_registry = oracle_registry
        self.engine_oracle = engine_oracle
        self._provider_binding = None
        if spec_provider is not None and oracle_registry is not None:
            from swarm_engine.governance.oracle_binding import (
                OracleBindingError)
            if not producer_id or not token:
                raise OracleBindingError(
                    "SynthesizingSource: bound mode requires an authenticated "
                    "producer for spec_provider -- an unattributed spec "
                    "provider is refused")
            if not oracle_registry.authenticate(producer_id, token):
                raise OracleBindingError(
                    "SynthesizingSource: spec_provider producer "
                    "authentication failed -- forged identity refused")
            pname = getattr(spec_provider, "__name__", "spec_provider")
            oracle_id, version = oracle_registry.register_oracle(
                producer_id, token, f"spec_provider.{pname}",
                spec_provider,
                input_contract="CapabilityRequirement -> CapabilitySpec",
                output_contract="CapabilitySpec (examples + constraints)",
                source="synthesizing source")
            self._provider_binding = {
                "oracle_id": oracle_id, "version": version,
                "producer_id": producer_id}

    def _bound_provider(self):
        """Re-verify the spec provider against its registration (O1
        pattern); wrap it so the requirement->spec mapping is recorded."""
        provider = self.spec_provider
        binding = self._provider_binding
        if (provider is None or self.oracle_registry is None
                or binding is None):
            return provider, None
        from swarm_engine.governance.binding_helpers import (
            BoundCallable, verify_live_callable)
        verify_live_callable(self.oracle_registry, binding["oracle_id"],
                             binding["version"], provider,
                             what="spec_provider")
        if self.engine_oracle is None:
            return provider, binding
        return BoundCallable(
            self.oracle_registry, self.engine_oracle, provider,
            binding["oracle_id"], binding["version"],
            binding["producer_id"], what="spec_provider"), binding

    def search(self, requirement: CapabilityRequirement) -> List[Candidate]:
        provider, binding = self._bound_provider()
        spec = (provider(requirement) if provider is not None
                else CapabilitySpec.from_requirement(requirement))
        # O15: record which spec was searched against and whose it was
        # (requirement digest -> spec digest, provider id/version).
        if (provider is not None and binding is not None
                and self.engine_oracle is not None):
            from swarm_engine.governance.binding_helpers import (
                canonical_digest)
            self.engine_oracle.evaluate(
                binding["oracle_id"],
                {"requirement_digest": canonical_digest(
                    requirement.as_dict())},
                {"spec_digest": canonical_digest(spec.as_dict()),
                 "n_examples": len(spec.examples)},
                input_ref=f"spec_provider:{requirement.name}",
                version=binding["version"],
                supplier_id=binding["producer_id"])
        return self.generate(spec)

    def _generate_affine_candidates(self, spec: CapabilitySpec, args: List[str],
                                    arg_list: str) -> List["Candidate"]:
        """Discover scale/offset directly from the supplied evidence for a
        single-numeric-input relationship, rather than requiring an affine
        skeleton with pre-baked constants.

        Generic and evidence-driven: this fits y = scale*x + offset from
        pairs of the CALLER'S OWN examples using ordinary linear algebra
        (two points determine a line), never from any domain-specific
        constant. If fewer than two numeric examples are available, or the
        function isn't single-argument-numeric, this contributes nothing.
        Every fit is generated as an ordinary Candidate and goes through
        the exact same independent validation (including held-out
        examples) as every other candidate — a fit that only explains the
        two points it was derived from and disagrees with a third example
        is rejected there, not here.
        """
        if len(args) != 1 or not spec.examples or len(spec.examples) < 2:
            return []
        numeric_examples = []
        for ex_args, ex_out in spec.examples:
            vals = list(ex_args.values())
            if len(vals) != 1:
                return []
            try:
                x, y = float(vals[0]), float(ex_out)
            except (TypeError, ValueError):
                return []
            numeric_examples.append((x, y))
        out: List[Candidate] = []
        # Try every distinct pair of examples as the fitting basis, not
        # just the first two — if the relationship really is affine, every
        # pair should agree on (approximately) the same scale/offset, and
        # trying more than one pair costs little while giving the
        # candidate pool several independently-derived (but likely
        # identical) hypotheses rather than a single fragile one.
        seen_fits = set()
        for i in range(len(numeric_examples)):
            for j in range(i + 1, len(numeric_examples)):
                (x1, y1), (x2, y2) = numeric_examples[i], numeric_examples[j]
                if x2 == x1:
                    continue
                scale = (y2 - y1) / (x2 - x1)
                offset = y1 - scale * x1
                fit_key = (round(scale, 6), round(offset, 6))
                if fit_key in seen_fits:
                    continue
                seen_fits.add(fit_key)
                v = args[0]
                code = (f"def capability({arg_list}):\n"
                       f"    return ({scale!r}) * {v} + ({offset!r})\n")
                out.append(Candidate(
                    name=f"gen_{spec.name}_affine_fit_{i}_{j}",
                    source="synthesized:affine_discovery", code=code,
                    notes=(f"affine fit scale={scale!r} offset={offset!r} "
                          f"discovered from examples[{i}],[{j}]")))
        return out

    def _generate_polynomial_candidates(self, spec: CapabilitySpec,
                                        args: List[str],
                                        arg_list: str) -> List["Candidate"]:
        """Discover polynomial coefficients directly from the caller's own
        evidence, generalizing _generate_affine_candidates beyond degree 1.

        Generic and evidence-driven: for each degree d in 2..4 (bounded by
        the example count, since d+1 distinct points determine a degree-d
        polynomial), fit y = c_d*x^d + ... + c_1*x + c_0 by least squares
        over ALL of the caller's examples, then emit the fit as ordinary
        source. A fit that does not explain the evidence dies at the
        worked-example prescreen or the IndependentValidator gauntlet --
        the fitter never certifies its own output. Degenerate fits whose
        leading coefficient vanishes are skipped (a lower degree, and
        ultimately the affine machinery, owns that class); coefficient
        magnitude is capped as a pathological-blowup guard, mirroring the
        quadratic probe's convention.

        This closes the skeleton grammar's genuine production gap: the
        fixed skeletons and unary stage compositions can express x^2 and
        2x^2 but no binary additive combination of derived terms with
        evidence-fitted coefficients (3x^2+2x+1 and every other polynomial
        of degree >= 2 were unreachable, not merely unenclosed).
        """
        if _np is None:
            return []
        if len(args) != 1 or not spec.examples or len(spec.examples) < 3:
            return []
        xs: List[float] = []
        ys: List[float] = []
        for ex_args, ex_out in spec.examples:
            vals = list(ex_args.values())
            if len(vals) != 1:
                return []
            try:
                xs.append(float(vals[0]))
                ys.append(float(ex_out))
            except (TypeError, ValueError):
                return []
        if len(set(xs)) < 3:
            return []
        _xs = _np.array(xs, dtype=float)
        _ys = _np.array(ys, dtype=float)
        out: List[Candidate] = []
        v = args[0]
        # Degree bound: d+1 points determine a degree-d polynomial, so the
        # example count is the natural bound; hard cap at 4 keeps the
        # Vandermonde system well-conditioned for typical evidence sizes.
        for degree in range(2, min(5, len(xs))):
            try:
                vand = _np.vander(_xs, degree + 1, increasing=True)
                coef, _, _, _ = _np.linalg.lstsq(vand, _ys, rcond=None)
            except Exception:
                continue
            coef = [round(float(c), 6) for c in coef]
            if abs(coef[-1]) < 1e-9:
                continue  # degenerate: a lower degree owns this class
            if max(abs(c) for c in coef) > 1e6:
                continue  # pathological-blowup guard
            terms = []
            for power in range(degree, -1, -1):
                c = coef[power]
                if power == 0:
                    terms.append(f"({c!r})")
                elif power == 1:
                    terms.append(f"({c!r}) * {v}")
                else:
                    terms.append(f"({c!r}) * {v} ** {power}")
            code = (f"def capability({arg_list}):\n"
                    f"    return {' + '.join(terms)}\n")
            out.append(Candidate(
                name=f"gen_{spec.name}_poly_fit_d{degree}",
                source="synthesized:polynomial_discovery", code=code,
                notes=(f"polynomial fit degree={degree} "
                       f"coef={coef} discovered from "
                       f"{len(xs)} examples")))
        return out

    def _generate_elementwise_candidates(
            self, spec: CapabilitySpec, args: List[str], arg_list: str,
            report: GenerationReport) -> List["Candidate"]:
        """Uniform elementwise transforms over (nested) sequences, induced
        from evidence.

        Generic and evidence-driven: fires only when every worked example
        maps a list input to a same-shaped list output (uniform structure —
        lengths match at every level). The element-operation vocabulary is
        the existing single/two-argument numeric skeleton set, reused via
        _skeleton_expression so any future skeleton is automatically
        liftable; constants for two-argument skeletons are mined from the
        caller's own scalar leaves; nesting depth (1 or 2) is mined from
        example shapes. Covers the uniform-depth elementwise class
        (recursive structural transforms such as increment-every-leaf)
        with no goal-text or task-specific knowledge. Every candidate is an
        ordinary Candidate through the same scanner, worked-example
        prescreen and independent validation — a wrong lift dies there,
        not here.
        """
        if len(args) != 1 or not spec.examples:
            return []
        key = args[0]

        def _is_num(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool)

        def _uniform_depth(in_v, out_v):
            # 1 or 2 for uniform numeric structure, else None.
            if not isinstance(in_v, list) or not isinstance(out_v, list):
                return None
            if len(in_v) != len(out_v):
                return None
            if all(_is_num(v) for v in in_v) and all(_is_num(v) for v in out_v):
                return 1
            if (in_v and all(isinstance(v, list) for v in in_v)
                    and all(isinstance(v, list) for v in out_v)
                    and all(len(a) == len(b) for a, b in zip(in_v, out_v))
                    and all(_is_num(x) for sub in in_v for x in sub)
                    and all(_is_num(x) for sub in out_v for x in sub)):
                return 2
            return None

        depths = set()
        for ex_args, ex_out in spec.examples:
            d = _uniform_depth(ex_args.get(key), ex_out)
            if d is None:
                return []
            depths.add(d)
        if len(depths) != 1:
            return []  # non-uniform structure: not an elementwise map
        depth = depths.pop()
        var = args[0]

        # Constants mined from the caller's own scalar leaves.
        consts: List[Any] = []
        seen = set()
        for ex_args, ex_out in spec.examples:
            stack = [ex_args.get(key), ex_out]
            while stack:
                v = stack.pop()
                if _is_num(v):
                    c = v if isinstance(v, float) and v != int(v) else int(v)
                    if c not in seen and abs(c) <= 1000:
                        seen.add(c)
                        consts.append(c)
                elif isinstance(v, list):
                    stack.extend(v)
        for n in (0, 1, 2):
            if n not in seen:
                consts.append(n)
        consts = consts[:12]

        # Element vocabulary: reuse the existing numeric skeleton set.
        unary_exprs: List[Tuple[str, str]] = []
        binary_exprs: List[Tuple[str, str]] = []
        for name, template in _SKELETONS:
            if name not in _NUMERIC_SKELETONS:
                continue
            expr = _skeleton_expression(template)
            if expr is None or "{a0}" not in expr:
                continue
            if "{a1}" in template:
                binary_exprs.append((name, expr))
            else:
                unary_exprs.append((name, expr))

        out: List["Candidate"] = []
        for name, expr in unary_exprs:
            e = expr.replace("{a0}", "v")
            out.extend(self._elementwise_forms(
                spec, var, arg_list, depth, e, f"elem_{name}", report))
        for name, expr in binary_exprs:
            for ci, c in enumerate(consts):
                e1 = expr.replace("{a0}", "v").replace("{a1}", repr(c))
                out.extend(self._elementwise_forms(
                    spec, var, arg_list, depth, e1,
                    f"elem_{name}_c{ci}", report))
                e2 = expr.replace("{a0}", repr(c)).replace("{a1}", "v")
                out.extend(self._elementwise_forms(
                    spec, var, arg_list, depth, e2,
                    f"elem_{name}_r{ci}", report))
        return out

    @staticmethod
    def _elementwise_forms(spec: CapabilitySpec, var: str, arg_list: str,
                           depth: int, elem_expr: str, tag: str,
                           report: GenerationReport) -> List["Candidate"]:
        if depth == 1:
            comp = f"[{elem_expr} for v in {var}]"
        else:
            comp = f"[[{elem_expr} for v in sub] for sub in {var}]"
        code = f"def capability({arg_list}):\n    return {comp}\n"
        report.attempted.append(tag)
        return [Candidate(
            name=f"gen_{spec.name}_{tag}_{depth}d",
            source="synthesized:elementwise_induction", code=code,
            notes=(f"elementwise lift depth={depth} expr={elem_expr!r} "
                   f"induced from evidence shapes"))]

    def _generate_recursive_elementwise_candidates(
            self, spec: CapabilitySpec, args: List[str], arg_list: str,
            report: GenerationReport) -> List["Candidate"]:
        """Recursive elementwise transforms over arbitrarily nested lists.

        2026-09-22 (F3 R13): R2 covers uniform depth 1-2 with unrolled
        comprehensions; arbitrary/mixed-depth nesting was absent (proven:
        depth-3 increment failed on every route). This induction is
        evidence-driven: it fires only when examples show nested list
        structure at mixed depths or depth > 2, collects every
        (in_leaf, out_leaf) pair by recursive shape-zip, and emits
        GENUINELY RECURSIVE code only if a single skeleton transform from
        the shared numeric vocabulary explains ALL pairs. No surviving
        transform means no candidate (fail closed). Same scanner,
        prescreen and independent validation as every other induction.
        """
        if len(args) != 1 or not spec.examples:
            return []
        key = args[0]

        def _is_num(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool)

        def _zip_leaves(in_v, out_v, pairs):
            if isinstance(in_v, list) and isinstance(out_v, list):
                if len(in_v) != len(out_v):
                    return False
                for a, b in zip(in_v, out_v):
                    if not _zip_leaves(a, b, pairs):
                        return False
                return True
            if isinstance(in_v, list) != isinstance(out_v, list):
                return False
            if not (_is_num(in_v) and _is_num(out_v)):
                return False
            pairs.append((in_v, out_v))
            return True

        def _max_depth(v):
            if not isinstance(v, list) or not v:
                return 0
            return 1 + max((_max_depth(e) for e in v), default=0)

        all_pairs: List[Tuple[Any, Any]] = []
        depths = set()
        for ex_args, ex_out in spec.examples:
            in_v = ex_args.get(key)
            if not isinstance(in_v, list) or not isinstance(ex_out, list):
                return []
            pairs: List[Tuple[Any, Any]] = []
            if not _zip_leaves(in_v, ex_out, pairs) or not pairs:
                return []
            all_pairs.extend(pairs)
            depths.add(_max_depth(in_v))
        # Uniform depth 1-2 is R2's territory; recursion earns its keep on
        # mixed depths or depth > 2.
        if len(depths) == 1 and depths <= {1, 2}:
            return []

        # Constants mined from the caller's own scalar leaves.
        consts: List[Any] = []
        seen = set()
        for iv, ov in all_pairs:
            for v in (iv, ov):
                c = v if isinstance(v, float) and v != int(v) else int(v)
                if c not in seen and abs(c) <= 1000:
                    seen.add(c)
                    consts.append(c)
        for n in (0, 1, 2):
            if n not in seen:
                consts.append(n)
        consts = consts[:12]

        def _fits(expr_v):
            # expr_v: python expression in variable v
            try:
                fn = eval(f"lambda v: {expr_v}")
            except Exception:
                return False
            for iv, ov in all_pairs:
                try:
                    if fn(iv) != ov:
                        return False
                except Exception:
                    return False
            return True

        transforms: List[Tuple[str, str]] = []
        for name, template in _SKELETONS:
            if name not in _NUMERIC_SKELETONS:
                continue
            expr = _skeleton_expression(template)
            if expr is None or "{a0}" not in expr:
                continue
            if "{a1}" in template:
                for ci, c in enumerate(consts):
                    e1 = expr.replace("{a0}", "v").replace("{a1}", repr(c))
                    if _fits(e1):
                        transforms.append((f"elem_{name}_c{ci}", e1))
                    e2 = expr.replace("{a0}", repr(c)).replace("{a1}", "v")
                    if _fits(e2):
                        transforms.append((f"elem_{name}_r{ci}", e2))
            else:
                e = expr.replace("{a0}", "v")
                if _fits(e):
                    transforms.append((f"elem_{name}", e))

        out: List["Candidate"] = []
        for tag, e in transforms:
            code = (f"def capability({arg_list}):\n"
                    f"    def rec(v):\n"
                    f"        if isinstance(v, list):\n"
                    f"            return [rec(x) for x in v]\n"
                    f"        return {e}\n"
                    f"    return rec({key})\n")
            report.attempted.append(f"rec_{tag}")
            out.append(Candidate(
                name=f"gen_{spec.name}_rec_{tag}",
                source="synthesized:recursive_elementwise_induction",
                code=code,
                notes=(f"recursive elementwise expr={e!r} over mixed/"
                       f"deep nesting; induced from evidence leaf pairs")))
        return out

    def _generate_regroup_candidates(
            self, spec: CapabilitySpec, args: List[str], arg_list: str,
            report: GenerationReport) -> List["Candidate"]:
        """Key-based regrouping with projection, induced from evidence.

        Generic and evidence-driven: fires only when every worked example
        maps a list of uniform dicts (rows) to a dict. The hypothesis class
        is fixed and general — group rows by one field, collect another
        field's values (or whole rows) — while the key/value fields are
        discovered from the caller's own examples: a (key_field,
        value_field) pair is emitted as a candidate only if regrouping with
        it reproduces EVERY example exactly. This is discovery, not answer
        injection — the same pattern as the affine/polynomial fitters — and
        every candidate still faces the scanner, prescreen and independent
        validation. Covers the regroup/project structural-algebra class
        (e.g. rows -> {key: [values]}) with no goal-text or field-name
        knowledge baked in.
        """
        if len(args) != 1 or not spec.examples:
            return []
        key = args[0]
        rows_per_ex = []
        for ex_args, ex_out in spec.examples:
            in_v = ex_args.get(key)
            if (not isinstance(in_v, list) or not in_v
                    or not all(isinstance(r, dict) for r in in_v)
                    or not isinstance(ex_out, dict)):
                return []
            fields = set(in_v[0].keys())
            if not fields or any(set(r.keys()) != fields for r in in_v):
                return []
            rows_per_ex.append((in_v, ex_out))
        fields = sorted(set(rows_per_ex[0][0][0].keys()))
        # All examples must share the same row fields for one induction.
        if any(set(r.keys()) != set(fields)
               for in_v, _ in rows_per_ex for r in in_v):
            return []

        def _regroup(rows, kf, vf):
            groups: Dict[Any, List[Any]] = {}
            for row in rows:
                k = row[kf]
                groups.setdefault(k, []).append(row if vf is None else row[vf])
            return groups

        out: List["Candidate"] = []
        var = args[0]
        for kf in fields:
            for vf in fields + [None]:
                try:
                    if not all(_regroup(in_v, kf, vf) == ex_out
                               for in_v, ex_out in rows_per_ex):
                        continue
                except Exception:
                    continue
                tag = f"regroup_{kf}_{vf if vf is not None else 'row'}"
                if vf is None:
                    body = (f"    groups = {{}}\n"
                            f"    for row in {var}:\n"
                            f"        groups.setdefault(row[{kf!r}], []).append(row)\n"
                            f"    return groups\n")
                else:
                    body = (f"    groups = {{}}\n"
                            f"    for row in {var}:\n"
                            f"        groups.setdefault(row[{kf!r}], []).append(row[{vf!r}])\n"
                            f"    return groups\n")
                code = f"def capability({arg_list}):\n{body}"
                report.attempted.append(tag)
                vf_label = repr(vf) if vf is not None else "whole row"
                out.append(Candidate(
                    name=f"gen_{spec.name}_{tag}",
                    source="synthesized:regroup_induction", code=code,
                    notes=(f"regroup by field {kf!r} collect {vf_label}; "
                           f"discovered from evidence shapes")))
        return out

    def _generate_zipwise_candidates(
            self, spec: CapabilitySpec, args: List[str], arg_list: str,
            report: GenerationReport) -> List["Candidate"]:
        """Elementwise combination of two parallel lists, induced from evidence.

        Generic and evidence-driven: fires only when every worked example maps
        two equal-length numeric lists to a same-length numeric list. The
        hypothesis class is fixed and general — zip the inputs, combine each
        pair with one skeleton expression — while the combining operation and
        any constants are discovered from the caller's own scalar pairs: a
        skeleton is emitted only if it reproduces EVERY pair of EVERY example
        exactly. Covers the binary structural-algebra class (e.g. pairwise
        sum/product) with no goal-text knowledge. Same discovery pattern as
        the affine fitters; same downstream validation.
        """
        if len(args) != 2 or not spec.examples:
            return []
        pairs: List[Tuple[float, float, float]] = []
        for ex_args, ex_out in spec.examples:
            a, b = ex_args.get(args[0]), ex_args.get(args[1])
            if (not isinstance(a, list) or not isinstance(b, list)
                    or not a or len(a) != len(b)
                    or not isinstance(ex_out, list)
                    or len(ex_out) != len(a)):
                return []
            for av, bv, ov in zip(a, b, ex_out):
                if (isinstance(av, bool) or isinstance(bv, bool)
                        or isinstance(ov, bool)
                        or not isinstance(av, (int, float))
                        or not isinstance(bv, (int, float))
                        or not isinstance(ov, (int, float))):
                    return []
                pairs.append((av, bv, ov))
        consts = sorted({v for trip in pairs for v in trip
                         if isinstance(v, (int, float))
                         and not isinstance(v, bool)
                         and abs(v) < 10 ** 6})
        skeletons = ["v + w", "v - w", "w - v", "v * w"]
        for c in consts:
            cr = repr(c)
            skeletons.extend([f"v + {cr}", f"w + {cr}", f"v * {cr}",
                              f"w * {cr}", f"v * {cr} + w", f"v + w * {cr}",
                              f"v * w + {cr}"])
        out: List["Candidate"] = []
        va, vb = args[0], args[1]
        for sk in skeletons:
            body = sk.replace("v", "V").replace("w", "W")
            try:
                fn = eval(f"lambda V, W: {body}", {"__builtins__": {}})
            except Exception:
                continue
            try:
                if not all(fn(av, bv) == ov for av, bv, ov in pairs):
                    continue
            except Exception:
                continue
            expr = sk.replace("v", "x").replace("w", "y")
            code = (f"def capability({arg_list}):\n"
                    f"    return [{expr} for x, y in zip({va}, {vb})]\n")
            tag = f"zipwise_{len(out)}"
            report.attempted.append(tag)
            out.append(Candidate(
                name=f"gen_{spec.name}_{tag}",
                source="synthesized:zipwise_induction", code=code,
                notes=(f"zipwise combine {sk}; discovered from evidence "
                       f"scalar pairs")))
        return out

    def _generate_transpose_candidates(
            self, spec: CapabilitySpec, args: List[str], arg_list: str,
            report: GenerationReport) -> List["Candidate"]:
        """Matrix transpose, induced from evidence.

        Generic and evidence-driven: fires only when every worked example maps
        a rectangular matrix (nonempty list of equal-length nonempty lists) to
        its transpose — verified by computing the transpose from the input and
        requiring exact equality with the expected output on EVERY example.
        Emits the general transpose comprehension, never a per-case table.
        """
        if len(args) != 1 or not spec.examples:
            return []
        key = args[0]
        for ex_args, ex_out in spec.examples:
            m = ex_args.get(key)
            if (not isinstance(m, list) or not m
                    or not all(isinstance(r, list) and r for r in m)
                    or not isinstance(ex_out, list)):
                return []
            w = len(m[0])
            if any(len(r) != w for r in m):
                return []
            expect_t = [[r[i] for r in m] for i in range(w)]
            if (len(ex_out) != w
                    or any(not isinstance(r, list) or len(r) != len(m)
                           for r in ex_out)
                    or ex_out != expect_t):
                return []
        code = (f"def capability({arg_list}):\n"
                f"    return [[row[i] for row in {key}] "
                f"for i in range(len({key}[0]))]\n")
        report.attempted.append("transpose")
        return [Candidate(
            name=f"gen_{spec.name}_transpose",
            source="synthesized:transpose_induction", code=code,
            notes="matrix transpose; verified against evidence shapes")]

    def _generate_fold_candidates(
            self, spec: CapabilitySpec, args: List[str], arg_list: str,
            report: GenerationReport) -> List["Candidate"]:
        """Scalar fold over a numeric list, induced from evidence.

        Generic and evidence-driven: fires only when every worked example maps
        a numeric list to a scalar. The hypothesis class is fixed and general
        — sum / product / max / min folds — while the choice is discovered
        from the caller's own examples: a fold is emitted only if it
        reproduces EVERY example exactly. Covers the fold/reduce
        structural-algebra class with no goal-text knowledge; a degenerate
        contains-style plan cannot pass because the emitted code is a genuine
        fold, and with n>=k+2 evidence the validator's held-out discriminates.
        """
        if len(args) != 1 or not spec.examples:
            return []
        key = args[0]

        def _num(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool)

        seqs = []
        for ex_args, ex_out in spec.examples:
            xs, o = ex_args.get(key), ex_out
            if not isinstance(xs, list) or not all(_num(v) for v in xs):
                return []
            if not _num(o):
                return []
            seqs.append((xs, o))
        out: List["Candidate"] = []

        def _pfold(xs):
            acc = 1
            for v in xs:
                acc = acc * v
            return acc

        def _emit(tag, code, note):
            report.attempted.append(tag)
            out.append(Candidate(
                name=f"gen_{spec.name}_{tag}",
                source="synthesized:fold_induction", code=code, notes=note))

        if all(sum(xs) == o for xs, o in seqs):
            _emit("fold_sum",
                  f"def capability({arg_list}):\n    return sum({key})\n",
                  "sum fold; discovered from evidence")
        if all(_pfold(xs) == o for xs, o in seqs):
            _emit("fold_product",
                  f"def capability({arg_list}):\n    acc = 1\n"
                  f"    for v in {key}:\n        acc = acc * v\n"
                  f"    return acc\n",
                  "product fold; discovered from evidence")
        if all(xs for xs, _ in seqs):
            if all(max(xs) == o for xs, o in seqs):
                _emit("fold_max",
                      f"def capability({arg_list}):\n    return max({key})\n",
                      "max fold; discovered from evidence")
            if all(min(xs) == o for xs, o in seqs):
                _emit("fold_min",
                      f"def capability({arg_list}):\n    return min({key})\n",
                      "min fold; discovered from evidence")
        return out

    def _generate_projection_candidates(
            self, spec: CapabilitySpec, args: List[str], arg_list: str,
            report: GenerationReport) -> List["Candidate"]:
        """Index/key projection into a nested structure, induced from evidence.

        Generic and evidence-driven: fires only when every worked example maps
        a composite input (list/tuple/dict) to a leaf value. The hypothesis
        class is fixed and general — "the output is the leaf at index path P
        of the input" — while P is discovered from the caller's own examples:
        all root-to-leaf paths reaching a leaf equal to the expected output
        are collected per example, intersected across examples, and a
        candidate is emitted for each surviving path. No surviving path means
        no candidate (fail closed). Covers the projection leaves that
        structural decomposition produces (e.g. m[1][0]) with no goal-text
        knowledge; same downstream validation as every other induction.
        """
        if len(args) != 1 or not spec.examples:
            return []
        key = args[0]

        def _paths(node, target, prefix):
            found = []
            if isinstance(node, (list, tuple)):
                for i, v in enumerate(node):
                    found.extend(_paths(v, target, prefix + [i]))
            elif isinstance(node, dict):
                for k, v in node.items():
                    found.extend(_paths(v, target, prefix + [k]))
            elif not isinstance(node, (list, tuple, dict, set, frozenset)):
                if node == target and type(node) is type(target):
                    found.append(tuple(prefix))
            return found

        common = None
        for ex_args, ex_out in spec.examples:
            in_v = ex_args.get(key)
            if not isinstance(in_v, (list, tuple, dict)):
                return []
            if isinstance(ex_out, (list, tuple, dict, set, frozenset)):
                return []
            ps = set(_paths(in_v, ex_out, []))
            if not ps:
                return []
            common = ps if common is None else (common & ps)
            if not common:
                return []
        out: List["Candidate"] = []
        for path in sorted(common, key=repr):
            access = "".join(f"[{p!r}]" for p in path)
            code = (f"def capability({arg_list}):\n"
                    f"    return {key}{access}\n")
            tag = f"proj_{len(out)}"
            report.attempted.append(tag)
            out.append(Candidate(
                name=f"gen_{spec.name}_{tag}",
                source="synthesized:projection_induction", code=code,
                notes=(f"index projection {access}; discovered from "
                       f"evidence leaf paths")))
        return out

    def generate(self, spec: CapabilitySpec) -> List[Candidate]:
        report = GenerationReport(method="skeleton+composition synthesis")
        args = spec.input_names or ["value"]
        # NOTE: Input-dependency reordering was removed from this path
        # (2026-09-13). It broke capability admission for multi-input
        # requirements by changing the plan's input order. The
        # GeneralSynthesizer (path-2) provides the same guidance via pool
        # ordering without affecting the persisted plan structure.
        arg_list = ", ".join(args)
        substitutions = {"args": arg_list}
        for index, name in enumerate(args):
            substitutions[f"a{index}"] = name
        # Skeletons referencing a1 need a second argument to be applicable.
        candidates: List[Candidate] = []

        candidates.extend(self._generate_affine_candidates(spec, args, arg_list))
        candidates.extend(self._generate_polynomial_candidates(spec, args, arg_list))
        candidates.extend(self._generate_elementwise_candidates(
            spec, args, arg_list, report))
        candidates.extend(self._generate_regroup_candidates(
            spec, args, arg_list, report))
        candidates.extend(self._generate_zipwise_candidates(
            spec, args, arg_list, report))
        candidates.extend(self._generate_transpose_candidates(
            spec, args, arg_list, report))
        candidates.extend(self._generate_fold_candidates(
            spec, args, arg_list, report))
        candidates.extend(self._generate_recursive_elementwise_candidates(
            spec, args, arg_list, report))
        candidates.extend(self._generate_projection_candidates(
            spec, args, arg_list, report))

        for name, template in _SKELETONS:
            if "{a1}" in template and len(args) < 2:
                continue
            try:
                code = template.format(**substitutions)
            except KeyError:
                continue
            report.attempted.append(name)
            candidates.append(Candidate(
                name=f"gen_{spec.name}_{name}", source="synthesized:skeleton",
                code=code, notes=f"skeleton {name}"))

        # Two-stage pipelines over the first argument (existing, unchanged).
        primary = args[0]
        for first, second in itertools.permutations(_STAGES, 2):
            if len(candidates) >= self.max_candidates:
                break
            inner = first[1].format(v=primary)
            outer = second[1].format(v=inner)
            code = f"def capability({arg_list}):\n    return {outer}\n"
            report.attempted.append(f"{first[0]}->{second[0]}")
            candidates.append(Candidate(
                name=f"gen_{spec.name}_{first[0]}_{second[0]}",
                source="synthesized:composition", code=code,
                notes=f"composition {first[0]} then {second[0]}"))

        # General skeleton-then-transform composition: ANY skeleton's
        # result (including a multi-argument skeleton's, e.g.
        # subtract_two(a,b)) can feed any type-compatible outer transform,
        # not only sequence stages chained onto args[0] alone. Found
        # necessary directly in M+28.24's forensic pass: subtract_two and
        # abs_value both existed as independent skeletons, but nothing in
        # the prior mechanism ever composed them together, because
        # composition only ever started from args[0]. This loop is
        # generic — it does not special-case subtraction or absolute
        # value; it tries every skeleton against every type-compatible
        # outer transform and lets independent validation decide which
        # (if any) explain the evidence. Type tags prune combinations that
        # could never type-check (e.g. never .upper() a numeric result)
        # rather than wasting budget generating and rejecting them later.
        for inner_name, inner_template in _SKELETONS:
            if len(candidates) >= self.max_candidates:
                break
            if "{a1}" in inner_template and len(args) < 2:
                continue
            if inner_name == "identity":
                continue  # already covered directly by the skeleton itself
            inner_kind = ("numeric" if inner_name in _NUMERIC_SKELETONS
                         else "sequence" if inner_name in _SEQUENCE_SKELETONS
                         else None)
            if inner_kind is None:
                continue
            try:
                inner_expr = inner_template.format(**substitutions)
            except KeyError:
                continue
            # extract just the return expression, not the full def, since
            # this inner result needs to be embedded as a sub-expression
            inner_value = inner_expr.split("return ", 1)[-1].strip()
            outer_pool = _NUMERIC_STAGES if inner_kind == "numeric" else _STAGES
            for outer_name, outer_template in outer_pool:
                if len(candidates) >= self.max_candidates:
                    break
                outer_expr = outer_template.format(v=f"({inner_value})")
                code = f"def capability({arg_list}):\n    return {outer_expr}\n"
                report.attempted.append(f"{inner_name}->{outer_name}")
                candidates.append(Candidate(
                    name=f"gen_{spec.name}_{inner_name}_{outer_name}",
                    source="synthesized:general_composition", code=code,
                    notes=f"general composition: {inner_name} then {outer_name}"))

        candidates = candidates[: self.max_candidates]
        report.produced = len(candidates)
        if not spec.examples:
            report.notes.append(
                "no examples supplied: candidates cannot be discriminated, so "
                "none should be admitted on behavioural grounds")
        self.last_report = report
        return candidates


class DelegatingSource(CandidateSource):
    """Hands the problem to another agent or model.

    The delegate is injected rather than assumed, so this works with a local
    agent, a remote model, or a test double, and the pipeline treats whatever
    comes back as an untrusted candidate exactly like any other. Delegation
    changes who wrote the code, not how much it is believed.
    """
    requires_effect = None

    def __init__(self, delegate: Optional[Callable[[CapabilitySpec], List[str]]] = None,
                 producer_id: Optional[str] = None,
                 token: Optional[str] = None,
                 oracle_registry=None, engine_oracle=None,
                 delegate_id: Optional[str] = None):
        self.delegate = delegate
        # O16: the delegate is a PRODUCER, not a verdict oracle -- candidates
        # are still independently validated downstream. What gets bound is
        # the delegate's identity: which model produced which candidates.
        # Binding changes no admit/reject decision.
        self.oracle_registry = oracle_registry
        self.engine_oracle = engine_oracle
        self._delegate_binding = None
        if delegate is not None and oracle_registry is not None:
            from swarm_engine.governance.oracle_binding import (
                OracleBindingError)
            if not producer_id or not token:
                raise OracleBindingError(
                    "DelegatingSource: bound mode requires an authenticated "
                    "producer for the delegate -- an unattributed delegate "
                    "is refused (delegate identity is provenance)")
            if not oracle_registry.authenticate(producer_id, token):
                raise OracleBindingError(
                    "DelegatingSource: delegate producer authentication "
                    "failed -- forged identity refused")
            dname = delegate_id or getattr(delegate, "__name__", "delegate")
            oracle_id, version = oracle_registry.register_oracle(
                producer_id, token, f"delegate.{dname}", delegate,
                input_contract="CapabilitySpec -> List[str]",
                output_contract="candidate code strings (untrusted)",
                source="delegating source")
            self._delegate_binding = {
                "oracle_id": oracle_id, "version": version,
                "producer_id": producer_id,
                "delegate_id": f"delegate.{dname}"}

    def search(self, requirement: CapabilityRequirement) -> List[Candidate]:
        if self.delegate is None:
            return []
        spec = CapabilitySpec.from_requirement(requirement)
        delegate = self.delegate
        binding = self._delegate_binding
        if self.oracle_registry is not None and binding is not None:
            # O1 pattern: re-digest the live delegate against its registered
            # identity before it produces anything.
            from swarm_engine.governance.binding_helpers import (
                BoundCallable, verify_live_callable)
            verify_live_callable(self.oracle_registry, binding["oracle_id"],
                                 binding["version"], delegate,
                                 what=binding["delegate_id"])
            if self.engine_oracle is not None:
                delegate = BoundCallable(
                    self.oracle_registry, self.engine_oracle, delegate,
                    binding["oracle_id"], binding["version"],
                    binding["producer_id"],
                    what=binding["delegate_id"])
        out = []
        for index, code in enumerate(delegate(spec) or []):
            out.append(Candidate(name=f"delegated_{requirement.name}_{index}",
                                 source="delegated:agent", code=code,
                                 notes="supplied by a delegate; untrusted"))
        # O16 provenance: (spec digest -> candidate code digests,
        # delegate id/version), chained. Admission still rests entirely on
        # downstream independent validation.
        if (binding is not None and self.engine_oracle is not None):
            from swarm_engine.governance.binding_helpers import (
                canonical_digest)
            self.engine_oracle.evaluate(
                binding["oracle_id"],
                {"spec_digest": canonical_digest(spec.as_dict())},
                {"candidate_digests": [canonical_digest(c.code)
                                       for c in out],
                 "n": len(out)},
                input_ref=f"delegation:{requirement.name}",
                version=binding["version"],
                supplier_id=binding["producer_id"])
        return out


class ExampleInducer:
    """Derives a lookup capability directly from worked examples.

    Deliberately weak, and labelled as such: a table of remembered answers
    generalises to nothing it has not seen. It exists so the engine has a
    fallback that is *honest about being a fallback* — the candidate raises on
    unseen input rather than guessing, so generalisation testing catches it
    instead of it silently passing as a real implementation.
    """

    def induce(self, spec: CapabilitySpec) -> Optional[Candidate]:
        if not spec.examples:
            return None
        table = {json.dumps(args, sort_keys=True, default=str): value
                 for args, value in spec.examples}
        args = spec.input_names or ["value"]
        arg_list = ", ".join(args)
        pairs = ", ".join(f"{k!r}: {v!r}" for k, v in table.items())
        code = (
            "import json\n"
            f"_TABLE = {{{pairs}}}\n"
            f"def capability({arg_list}):\n"
            f"    key = json.dumps({{{', '.join(f'{a!r}: {a}' for a in args)}}}, "
            "sort_keys=True, default=str)\n"
            "    if key not in _TABLE:\n"
            "        raise ValueError('no induced behaviour for this input')\n"
            "    return _TABLE[key]\n"
        )
        return Candidate(name=f"induced_{spec.name}", source="induced:examples",
                         code=code,
                         notes="induced from examples; does not generalise")
