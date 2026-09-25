"""
swarm_engine/acquisition/semantic.py

Semantic suitability and behavioural verification.

Two failure modes this module exists to prevent.

The first is the type-valid impostor: a plan whose signature fits the goal but
whose meaning has nothing to do with it. `platform_info` returns a value where
a transcription was wanted, and no type system will object. Structural
correctness is necessary and nowhere near sufficient, so `SemanticMatcher`
scores a capability against a goal on description overlap, input/output shape,
required effects, and the evidence already accumulated about it — and reports
*why* it scored what it did, so a rejection can be argued with.

The second is the candidate that runs without working. Executing successfully
is not the same as satisfying the objective, so `SemanticVerifier` runs a
suite of positive cases, negative cases (inputs that *must* fail), edge cases,
determinism checks and effect checks, and produces evidence that feeds trust
rather than a bare pass/fail.

Nothing here promotes anything on its own. Both produce evidence; the caller
decides.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

STOPWORDS = {
    "the", "a", "an", "of", "to", "for", "and", "or", "in", "on", "with",
    "from", "into", "then", "that", "this", "it", "is", "be", "by", "as",
    "my", "me", "please", "using", "use",
}


# Capability descriptions and goals routinely use different words for the same
# concept -- a goal asks for the "average", the primitive is called "mean".
# Without this, exact-token overlap scores a perfect match as a near miss, and
# the only ways to compensate would be lowering the threshold or inflating the
# template bonus, both of which would let genuine impostors through too.
SYNONYMS = {
    "average": {"mean", "avg"}, "mean": {"average", "avg"},
    "middle": {"median"}, "median": {"middle"},
    "largest": {"max", "maximum"}, "maximum": {"max", "largest", "biggest"},
    "smallest": {"min", "minimum"}, "minimum": {"min", "smallest"},
    "total": {"sum", "add"}, "sum": {"total", "add"},
    "order": {"sort", "sorted"}, "sort": {"order", "arrange"},
    "count": {"length", "size", "number"}, "length": {"count", "size"},
    "reverse": {"backwards", "invert"},
    "unique": {"distinct", "dedupe", "deduplicate"},
    "translate": {"translation"}, "transcribe": {"transcription", "speech"},
    "picture": {"image", "photo"}, "image": {"picture", "photo"},
    "fetch": {"download", "retrieve", "get"},
}


def tokens(text: str) -> set:
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
            if w and w not in STOPWORDS and len(w) > 2}


def expand(words: set) -> set:
    """Widen a token set with known synonyms, in both directions."""
    out = set(words)
    for word in words:
        out |= SYNONYMS.get(word, set())
    return out


# ---------------------------------------------------------------------------
# SEMANTIC MATCHING
# ---------------------------------------------------------------------------

@dataclass
class AcceptanceCriteria:
    """What it would take to believe a capability satisfies a goal."""
    required_keywords: List[str] = field(default_factory=list)
    forbidden_keywords: List[str] = field(default_factory=list)
    required_effects: List[str] = field(default_factory=list)
    expected_output_kind: Optional[str] = None
    min_trust: str = "TESTED"
    min_confidence: float = 0.0
    examples: List[Tuple[Dict[str, Any], Any]] = field(default_factory=list)


@dataclass
class MatchScore:
    score: float
    sufficient: bool
    reasons: List[str] = field(default_factory=list)
    against: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"score": round(self.score, 3), "sufficient": self.sufficient,
                "reasons": self.reasons, "against": self.against}


class SemanticMatcher:
    """Scores whether a capability plausibly satisfies a goal.

    The score is a weighted sum of independent signals, and every signal is
    reported. Deliberately conservative: absent evidence lowers the score
    rather than being treated as neutral, because the cost of wrongly deciding
    a goal is already covered is that the engine never acquires what it needs.
    """

    THRESHOLD = 0.55

    W_DESCRIPTION = 0.35
    W_EFFECTS = 0.20
    W_EVIDENCE = 0.20
    W_SHAPE = 0.15
    W_EXAMPLES = 0.10

    def __init__(self, provenance=None):
        self.provenance = provenance

    def score(self, goal: str, *, description: str = "",
              ops_used: Optional[Sequence[str]] = None,
              declared_effects: Optional[Sequence[str]] = None,
              inputs: Optional[Sequence[str]] = None,
              output_kind: Optional[str] = None,
              capability_id: Optional[str] = None,
              criteria: Optional[AcceptanceCriteria] = None,
              strategy: str = "") -> MatchScore:
        criteria = criteria or AcceptanceCriteria()
        goal_tokens = tokens(goal)
        reasons: List[str] = []
        against: List[str] = []
        total = 0.0

        # -- description / operation overlap --------------------------------
        described = expand(tokens(description) | {
            t for op in (ops_used or []) for t in tokens(op.replace(".", " "))})
        overlap = expand(goal_tokens) & described
        if goal_tokens:
            ratio = len(overlap) / len(goal_tokens)
        else:
            ratio = 0.0
        total += self.W_DESCRIPTION * ratio
        if overlap:
            reasons.append(f"vocabulary overlap with the goal: {sorted(overlap)}")
        else:
            against.append("nothing in the capability's description or operations "
                           "relates to the goal's vocabulary")

        # -- hard acceptance criteria ---------------------------------------
        haystack = f"{description} {' '.join(ops_used or [])}".lower()
        missing_required = [k for k in criteria.required_keywords
                            if k.lower() not in haystack]
        if missing_required:
            against.append(f"missing required concepts: {missing_required}")
        present_forbidden = [k for k in criteria.forbidden_keywords
                             if k.lower() in haystack]
        if present_forbidden:
            against.append(f"contains forbidden concepts: {present_forbidden}")

        # -- effects ---------------------------------------------------------
        declared = set(declared_effects or [])
        required = set(criteria.required_effects)
        if required:
            if required <= declared:
                total += self.W_EFFECTS
                reasons.append(f"declares the effects the goal needs: {sorted(required)}")
            else:
                against.append(f"lacks required effects: {sorted(required - declared)}")
        else:
            # No effect requirement: a pure capability is mildly preferable.
            total += self.W_EFFECTS * (1.0 if not declared else 0.6)

        # -- accumulated evidence -------------------------------------------
        evidence = 0.0
        if capability_id and self.provenance is not None:
            record = self.provenance.get(capability_id)
            if record is not None:
                evidence = record.confidence
                if record.trust.name == "QUARANTINED":
                    against.append("the capability is quarantined")
                    return MatchScore(0.0, False, reasons, against)
                reasons.append(f"trust={record.trust.name}, "
                               f"confidence={record.confidence:.2f}")
                if record.confidence < criteria.min_confidence:
                    against.append(
                        f"confidence {record.confidence:.2f} is below the required "
                        f"{criteria.min_confidence:.2f}")
        total += self.W_EVIDENCE * evidence

        # -- shape ------------------------------------------------------------
        if criteria.expected_output_kind:
            if output_kind == criteria.expected_output_kind:
                total += self.W_SHAPE
                reasons.append(f"output kind matches ({output_kind})")
            else:
                against.append(f"output kind {output_kind!r} is not the expected "
                               f"{criteria.expected_output_kind!r}")
        elif output_kind:
            total += self.W_SHAPE * 0.5

        # -- worked examples ---------------------------------------------------
        if criteria.examples:
            reasons.append(f"{len(criteria.examples)} acceptance example(s) supplied")
            total += self.W_EXAMPLES

        # A deliberate template match is evidence of intent, not merely of
        # structural fit, so it counts for something on its own.
        if strategy.startswith("template"):
            total += 0.15
            reasons.append("matched by a deliberate template, not blind search")

        sufficient = (total >= self.THRESHOLD and not against)
        return MatchScore(min(total, 1.0), sufficient, reasons, against)


# ---------------------------------------------------------------------------
# BEHAVIOURAL VERIFICATION
# ---------------------------------------------------------------------------

@dataclass
class Case:
    """One behavioural expectation."""
    args: Dict[str, Any]
    expect: Any = None
    predicate: Optional[Callable[[Any], bool]] = None
    must_fail: bool = False          # a negative case: this input MUST raise
    label: str = ""
    kind: str = "positive"           # positive | negative | edge

    def judge(self, ok: bool, value: Any, error: str) -> Tuple[bool, str]:
        if self.must_fail:
            if ok:
                return False, f"expected a failure but got {value!r}"
            return True, f"failed as required ({error[:60]})"
        if not ok:
            return False, f"raised: {error[:120]}"
        if self.predicate is not None:
            try:
                return bool(self.predicate(value)), "predicate"
            except Exception as exc:
                return False, f"predicate raised: {exc}"
        if self.expect is not None:
            # Exact equality is correct for everything except floats, where
            # it is a real bug: ordinary arithmetic (e.g. 1.8*37+32) rarely
            # lands on an exactly-representable value, so a genuinely
            # correct candidate can differ from the expected float by
            # rounding noise on the order of 1e-9 and still fail this
            # check under strict ==. A tolerance-based comparison for
            # floats does not weaken what counts as correct — a candidate
            # that is actually wrong is still rejected, since real errors
            # are far larger than floating-point noise; it only stops
            # penalizing candidates for arithmetic Python itself cannot
            # represent exactly.
            if isinstance(self.expect, float) and isinstance(value, (int, float)):
                import math
                ok = math.isclose(value, self.expect, rel_tol=1e-9, abs_tol=1e-9)
                return ok, f"expected {self.expect!r}, got {value!r}"
            return value == self.expect, f"expected {self.expect!r}, got {value!r}"
        return value is not None, "expected a non-null result"


@dataclass
class VerificationEvidence:
    passed: bool = False
    positive_passed: int = 0
    positive_total: int = 0
    negative_passed: int = 0
    negative_total: int = 0
    edge_passed: int = 0
    edge_total: int = 0
    deterministic: Optional[bool] = None
    effects_respected: Optional[bool] = None
    failures: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.positive_total + self.negative_total + self.edge_total

    @property
    def total_passed(self) -> int:
        return self.positive_passed + self.negative_passed + self.edge_passed

    def as_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "positive": f"{self.positive_passed}/{self.positive_total}",
            "negative": f"{self.negative_passed}/{self.negative_total}",
            "edge": f"{self.edge_passed}/{self.edge_total}",
            "deterministic": self.deterministic,
            "effects_respected": self.effects_respected,
            "failures": self.failures[:10], "notes": self.notes,
        }


class SemanticVerifier:
    """Runs a behavioural suite against a candidate inside isolation.

    Verification is stricter than "did it run". A candidate must satisfy the
    positive cases, *fail* the negative ones (a function that accepts garbage
    is not correct, it is merely permissive), handle the edge cases, and give
    the same answer twice for the same input. Non-determinism is reported
    rather than fatal, since some capabilities are legitimately random — but
    the caller gets told, because a non-deterministic capability cannot be
    benchmarked or regression-tested the same way.
    """

    def __init__(self, runner: Callable[[str, str, List[Dict[str, Any]]], Any]):
        # `runner` is anything with ProcessSandbox.run's contract, so the
        # verifier is indifferent to whether isolation is by process or
        # in-process; the caller chooses the strength.
        self.runner = runner

    def verify(self, code: str, entrypoint: str, cases: Sequence[Case],
               declared_effects: Optional[Sequence[str]] = None
               ) -> VerificationEvidence:
        evidence = VerificationEvidence()
        if not cases:
            evidence.notes.append("no cases supplied; nothing was verified")
            return evidence

        calls = [dict(c.args) for c in cases]
        report = self.runner(code, entrypoint, calls)

        if not getattr(report, "ok", False):
            evidence.failures.append(
                f"candidate could not be executed: {getattr(report, 'error', '')[:160]}")
            return evidence

        results = report.value or []
        if len(results) != len(cases):
            evidence.failures.append(
                f"expected {len(cases)} results, got {len(results)}")
            return evidence

        for case, result in zip(cases, results):
            ok = bool(result.get("ok"))
            value = result.get("value")
            error = str(result.get("error", ""))
            judged, why = case.judge(ok, value, error)

            bucket = case.kind if case.kind in ("positive", "negative", "edge") else "positive"
            if case.must_fail:
                bucket = "negative"
            setattr(evidence, f"{bucket}_total", getattr(evidence, f"{bucket}_total") + 1)
            if judged:
                setattr(evidence, f"{bucket}_passed",
                        getattr(evidence, f"{bucket}_passed") + 1)
            else:
                evidence.failures.append(f"{case.label or case.args}: {why}")

        # -- determinism: same input twice, same answer ----------------------
        positive = [c for c in cases if not c.must_fail]
        if positive:
            repeat = self.runner(code, entrypoint,
                                 [dict(positive[0].args), dict(positive[0].args)])
            if getattr(repeat, "ok", False) and len(repeat.value or []) == 2:
                first, second = repeat.value
                evidence.deterministic = (first.get("ok") == second.get("ok")
                                          and first.get("value") == second.get("value"))
                if not evidence.deterministic:
                    evidence.notes.append(
                        "candidate is non-deterministic; it cannot be benchmarked "
                        "or regression-tested by exact comparison")

        # -- effects: the isolation layer reports what it had to enforce -----
        caveats = getattr(report, "caveats", []) or []
        evidence.effects_respected = not any("forbid" in c.lower() for c in caveats)
        if caveats:
            evidence.notes.extend(caveats)

        evidence.passed = (not evidence.failures
                           and evidence.total_passed == evidence.total
                           and evidence.total > 0)
        return evidence
