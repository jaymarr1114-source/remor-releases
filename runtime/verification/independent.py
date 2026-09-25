"""
swarm_engine/verification/independent.py

Independent verification and adversarial validation.

The failure this module exists to prevent is circular validation: the
component that produced a solution also deciding the solution is good. When
that happens the system's confidence is a measure of its own enthusiasm, and
"SWarm improved itself because SWarm says so" is not a result.

So the roles are separated, and the separation is enforced rather than
described:

  Critic     - inspects the artifact statically, without running it
  Adversary  - *generates* hostile cases from the spec, not from the code
  Tester     - executes cases under isolation and reports raw outcomes
  Verifier   - checks whether claims follow from the evidence gathered
  Arbiter    - decides admission, and may only use evidence, never claims

Two rules give the separation teeth. First, the Adversary derives its cases
from the specification and the examples, never from the candidate's source —
otherwise it writes tests the candidate already passes. Second, the Arbiter
takes an `Evidence` object and has no access to the generator's own assertion
of correctness: a candidate cannot self-certify because there is no channel
through which its claim could arrive.

Layered levels follow the ten-level scheme: execute, unit, integrate, behave,
survive adversarial input, compare, regress, persist, justify, monitor.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from swarm_engine.acquisition.semantic import Case

try:
    from swarm_engine.governance.oracle_binding import (
        OracleRegistry, EngineOracleHandle, OracleBindingError,
        DECISION_VERIFICATION,
    )
except ImportError:  # pragma: no cover
    OracleRegistry = None  # type: ignore


class Level(Enum):
    SYNTACTIC = 1
    UNIT = 2
    INTEGRATION = 3
    BEHAVIOURAL = 4
    ADVERSARIAL = 5
    COMPARATIVE = 6
    REGRESSION = 7
    PERSISTENCE = 8
    PROVENANCE = 9
    MONITORING = 10


@dataclass
class Finding:
    level: Level
    passed: bool
    detail: str
    role: str = ""
    # Oracle bindings: each entry is {"oracle_id", "version", "eval_id"}
    # proving this finding came from a registered oracle evaluation over a
    # bound input. Findings at the Arbiter's required levels MUST carry at
    # least one verified binding, or decide() refuses the evidence.
    bindings: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"level": self.level.name, "passed": self.passed,
                "detail": self.detail[:220], "role": self.role}


@dataclass
class Evidence:
    """Everything observed about a candidate. The Arbiter sees only this."""
    findings: List[Finding] = field(default_factory=list)
    cases_run: int = 0
    cases_passed: int = 0
    adversarial_run: int = 0
    adversarial_passed: int = 0
    deterministic: Optional[bool] = None
    generalises: Optional[bool] = None
    # Aggregate of every oracle binding across findings (reporting).
    oracle_bindings: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, level: Level, passed: bool, detail: str, role: str,
            bindings: Optional[List[Dict[str, Any]]] = None) -> None:
        self.findings.append(Finding(level, passed, detail, role,
                                     bindings or []))
        for b in (bindings or []):
            self.oracle_bindings.append({**b, "level": level.name})

    def levels_passed(self) -> Dict[str, bool]:
        out: Dict[str, bool] = {}
        for finding in self.findings:
            out[finding.level.name] = out.get(finding.level.name, True) and finding.passed
        return out

    def failed_levels(self) -> List[str]:
        return [k for k, v in self.levels_passed().items() if not v]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cases": f"{self.cases_passed}/{self.cases_run}",
            "adversarial": f"{self.adversarial_passed}/{self.adversarial_run}",
            "deterministic": self.deterministic, "generalises": self.generalises,
            "levels": self.levels_passed(), "failed_levels": self.failed_levels(),
            "findings": [f.as_dict() for f in self.findings[:20]],
        }


# ---------------------------------------------------------------------------
# ROLES
# ---------------------------------------------------------------------------

class Critic:
    """Static inspection. Runs nothing; looks for what the code admits."""

    SUSPECT = {
        "bare except": ("except:", "except Exception:"),
        "hardcoded answer": ("return '", 'return "'),
        "no computation": (),
    }

    def review(self, code: str, spec) -> List[Finding]:
        findings: List[Finding] = []
        body = [l for l in code.splitlines() if l.strip()
                and not l.strip().startswith("#")]

        if any(tok in code for tok in ("except:", "except Exception:")):
            findings.append(Finding(
                Level.UNIT, False,
                "swallows exceptions broadly, which can turn a wrong answer "
                "into a silently accepted one", "critic"))

        # A candidate that ignores its inputs cannot be computing anything.
        for name in (spec.input_names or []):
            if name not in code.split("def capability", 1)[-1].split("\n", 1)[-1]:
                findings.append(Finding(
                    Level.UNIT, False,
                    f"never references its input {name!r}; the result cannot "
                    f"depend on it", "critic"))
                break

        if len(body) <= 1:
            findings.append(Finding(Level.SYNTACTIC, False,
                                    "has no body", "critic"))
        else:
            findings.append(Finding(Level.SYNTACTIC, True,
                                    "parses and has a body", "critic"))
        return findings


class Adversary:
    """Generates hostile cases from the *specification*, never from the code.

    Deriving cases from the candidate is how a test suite ends up confirming
    whatever the candidate happens to do. Working only from the spec and its
    examples means the Adversary can surprise the implementation — which is
    the entire point of having one.
    """

    def __init__(self, seed: int = 0):
        self.random = random.Random(seed)

    def generate(self, spec, limit: int = 12) -> List[Case]:
        cases: List[Case] = []
        args = spec.input_names or ["value"]

        sample = spec.examples[0][0] if spec.examples else {a: "ab" for a in args}
        for name, value in sample.items():
            for label, hostile in self._hostile_variants(value):
                probe = dict(sample)
                probe[name] = hostile
                cases.append(Case(args=probe, kind="edge",
                                  label=f"{name}={label}",
                                  predicate=lambda v: True))
                if len(cases) >= limit:
                    return cases
        return cases

    def _hostile_variants(self, value: Any) -> List[Tuple[str, Any]]:
        if isinstance(value, str):
            return [("empty", ""), ("whitespace", "   "),
                    ("very long", "x" * 5000), ("unicode", "日本語🙂"),
                    ("newlines", "a\nb\r\nc")]
        if isinstance(value, bool):
            return [("negated", not value)]
        if isinstance(value, (int, float)):
            return [("zero", 0), ("negative", -abs(value) - 1),
                    ("huge", 10 ** 12), ("float", float(value) + 0.5)]
        if isinstance(value, list):
            return [("empty", []), ("single", value[:1]),
                    ("duplicates", list(value) + list(value)),
                    ("large", list(value) * 500)]
        if isinstance(value, dict):
            return [("empty", {})]
        return [("none", None)]

    def _novel_value(self, value: Any, index: int) -> Any:
        """Novelize scalar leaves while preserving the example's structure.

        A novelty probe must be an input "of the right shape" (per the
        method contract below). The old code flattened every list to
        [7+i, 3+i, 11+i], so a genuinely shape-specific implementation
        (e.g. an elementwise transform over nested lists, whose contract
        is defined by nested examples) raised TypeError on every probe and
        was misclassified as a memorised table. Mirroring the structure
        recursively keeps the probe in-contract while the novel leaves
        keep it unseen.
        """
        if isinstance(value, str):
            return f"novel{index}probe"
        if isinstance(value, bool):
            return not value
        if isinstance(value, (int, float)):
            return value + 17 + index
        if isinstance(value, list):
            if not value:
                return [7 + index, 3 + index]
            return [self._novel_value(v, index) for v in value]
        if isinstance(value, tuple):
            return tuple(self._novel_value(v, index) for v in value)
        if isinstance(value, dict):
            return {k: self._novel_value(v, index)
                    for k, v in value.items()}
        if isinstance(value, (set, frozenset)):
            return type(value)(self._novel_value(v, index) for v in value)
        return value

    def novelty_probes(self, spec, count: int = 3) -> List[Case]:
        """Inputs of the right shape that appear in no example.

        This is what catches a memorised lookup table. Held-out examples do
        not: a table built from the whole specification contains them too, so
        it passes. But a table has no behaviour for input it has never seen and
        must raise, whereas any real implementation returns something. The
        probe asserts only that a value comes back -- it makes no claim about
        which value, because for novel input nobody knows the right answer.
        """
        args = spec.input_names or ["value"]
        sample = spec.examples[0][0] if spec.examples else {a: "ab" for a in args}
        seen = {json.dumps(a, sort_keys=True, default=str)
                for a, _ in (spec.examples or [])}

        probes: List[Case] = []
        for index in range(count):
            probe = dict(sample)
            for name, value in sample.items():
                probe[name] = self._novel_value(value, index)
            if json.dumps(probe, sort_keys=True, default=str) in seen:
                continue
            probes.append(Case(args=probe, kind="positive",
                               label=f"novel input {index}",
                               predicate=lambda v: v is not None))
        return probes

    def generalisation_cases(self, spec) -> List[Case]:
        """Cases drawn from the spec's own examples beyond the first.

        A candidate fitted to example one must still satisfy the rest, which
        is what separates an implementation from a lookup table.
        """
        return [Case(args=dict(args), expect=expected, kind="positive",
                     label="held-out example")
                for args, expected in list(spec.examples)[1:]]


class Tester:
    """Executes cases under isolation and reports raw outcomes only.

    Deliberately makes no judgement: mixing execution with interpretation is
    how "it ran" becomes "it worked".
    """

    def __init__(self, runner: Callable[[str, str, List[Dict[str, Any]]], Any]):
        self.runner = runner

    def run(self, code: str, entrypoint: str,
            cases: Sequence[Case]) -> Tuple[bool, List[Dict[str, Any]], str]:
        if not cases:
            return True, [], ""
        report = self.runner(code, entrypoint, [dict(c.args) for c in cases])
        if not getattr(report, "ok", False):
            return False, [], getattr(report, "error", "execution failed")
        return True, list(report.value or []), ""


class Verifier:
    """Turns raw outcomes into evidence at the appropriate level.

    ORACLE BINDING (2026-09-25): every judgment the Verifier makes is an
    oracle evaluation. Case oracles (the judge rule of each Case) and the
    static Critic are registered with the oracle registry, each judgment is
    recorded as a bound evaluation (oracle + input + result), and the
    resulting eval ids are attached to the findings. The Arbiter refuses any
    evidence whose required-level findings lack verified bindings -- so a
    fabricated Evidence object can no longer be decided as admitted.
    """

    def __init__(self, tester: Tester, adversary: Optional[Adversary] = None,
                 oracle_registry: Optional["OracleRegistry"] = None,
                 engine_oracle: Optional["EngineOracleHandle"] = None):
        self.tester = tester
        self.adversary = adversary or Adversary()
        self.oracle_registry = oracle_registry
        self.engine_oracle = engine_oracle
        self._critic_oracle: Optional[Tuple[str, int]] = None

    # -- oracle binding helpers -------------------------------------------
    def _bound(self) -> bool:
        return (self.oracle_registry is not None
                and self.engine_oracle is not None)

    def _register_case_oracle(self, case: Case) -> Optional[Tuple[str, int]]:
        """Register a Case's judge rule as an oracle. Deterministic name so
        identical cases reuse the same oracle lineage."""
        if not self._bound():
            return None
        import hashlib as _hl
        definition: Dict[str, Any] = {
            "kind": "case_judge", "args": case.args, "expect": case.expect,
            "must_fail": case.must_fail, "case_kind": case.kind,
            "label": case.label,
        }
        if case.predicate is not None:
            try:
                import inspect as _inspect
                definition["predicate_source"] = _inspect.getsource(
                    case.predicate)
            except (OSError, TypeError):
                definition["predicate_source"] = repr(case.predicate)[:300]
        name = "case:" + _hl.sha256(
            json.dumps(definition, sort_keys=True, separators=(",", ":"),
                       default=str).encode()).hexdigest()[:16]
        oracle_id, version = self.engine_oracle.register_oracle(
            name, definition, input_contract="case.args",
            output_contract="judge(ok, value, error) -> (passed, why)",
            source="verifier:case_oracle")
        self.engine_oracle.authorize_oracle(oracle_id, version,
                                            DECISION_VERIFICATION)
        return oracle_id, version

    def _bind_judgment(self, case: Case, judged: bool, why: str,
                       value: Any) -> List[Dict[str, Any]]:
        """Evaluate the case oracle over this judgment and return bindings."""
        reg = self._register_case_oracle(case)
        if reg is None:
            return []
        oracle_id, version = reg
        try:
            safe_value = repr(value)[:300]
        except Exception:
            safe_value = "<unrepresentable>"
        eval_id = self.engine_oracle.evaluate(
            oracle_id, {"args": case.args, "label": case.label},
            {"judged": judged, "why": why[:300], "value": safe_value},
            input_ref=f"verifier:{case.label[:60]}", version=version)
        return [{"oracle_id": oracle_id, "version": version,
                 "eval_id": eval_id}]

    def _bind_static(self, name: str, definition: Any, input_obj: Any,
                     result_obj: Any) -> List[Dict[str, Any]]:
        if not self._bound():
            return []
        oracle_id, version = self.engine_oracle.register_oracle(
            name, definition, source="verifier:static_oracle")
        self.engine_oracle.authorize_oracle(oracle_id, version,
                                            DECISION_VERIFICATION)
        eval_id = self.engine_oracle.evaluate(
            oracle_id, input_obj, result_obj, version=version)
        return [{"oracle_id": oracle_id, "version": version,
                 "eval_id": eval_id}]

    def verify(self, code: str, entrypoint: str, spec,
               cases: Sequence[Case]) -> Evidence:
        evidence = Evidence()

        import hashlib as _hl
        code_digest = _hl.sha256(code.encode("utf-8")).hexdigest()

        for finding in Critic().review(code, spec):
            bindings = self._bind_static(
                "critic:review", Critic.review,
                {"code_digest": code_digest},
                {"passed": finding.passed, "detail": finding.detail[:200]})
            finding.bindings = bindings
            evidence.findings.append(finding)
            for b in bindings:
                evidence.oracle_bindings.append(
                    {**b, "level": finding.level.name})

        # -- behavioural ----------------------------------------------------
        ok, results, error = self.tester.run(code, entrypoint, cases)
        if not ok:
            evidence.add(
                Level.SYNTACTIC, False, f"could not execute: {error}",
                "tester",
                bindings=self._bind_static(
                    "tester:execution", Tester.run,
                    {"code_digest": code_digest,
                     "n_cases": len(list(cases))},
                    {"ok": False, "error": error[:200]}))
            return evidence
        evidence.add(
            Level.SYNTACTIC, True, "executes under isolation", "tester",
            bindings=self._bind_static(
                "tester:execution", Tester.run,
                {"code_digest": code_digest, "n_cases": len(list(cases))},
                {"ok": True}))

        behavioural_bindings: List[Dict[str, Any]] = []
        for case, result in zip(cases, results):
            evidence.cases_run += 1
            judged, why = case.judge(bool(result.get("ok")), result.get("value"),
                                     str(result.get("error", "")))
            bindings = self._bind_judgment(case, judged, why,
                                           result.get("value"))
            behavioural_bindings.extend(bindings)
            if judged:
                evidence.cases_passed += 1
            else:
                evidence.add(Level.BEHAVIOURAL, False,
                             f"{case.label or case.args}: {why}", "verifier",
                             bindings=bindings)
        if evidence.cases_run and evidence.cases_passed == evidence.cases_run:
            evidence.add(Level.BEHAVIOURAL, True,
                         f"satisfied all {evidence.cases_run} behavioural cases",
                         "verifier", bindings=behavioural_bindings)

        # -- adversarial ------------------------------------------------------
        hostile = self.adversary.generate(spec)
        if hostile:
            ok, results, error = self.tester.run(code, entrypoint, hostile)
            if not ok:
                evidence.add(
                    Level.ADVERSARIAL, False,
                    f"hostile input crashed the run: {error}", "adversary",
                    bindings=self._bind_static(
                        "tester:execution", Tester.run,
                        {"code_digest": code_digest,
                         "n_cases": len(hostile), "kind": "hostile"},
                        {"ok": False, "error": error[:200]}))
            else:
                hostile_bindings: List[Dict[str, Any]] = []
                for case, result in zip(hostile, results):
                    evidence.adversarial_run += 1
                    # Surviving means terminating with either a value or a
                    # clean exception. A hang or a kill is not survival.
                    survived = result.get("ok") or bool(result.get("error"))
                    bindings = self._bind_judgment(
                        case, bool(survived),
                        "survived" if survived else "did not terminate",
                        result.get("value"))
                    hostile_bindings.extend(bindings)
                    if survived:
                        evidence.adversarial_passed += 1
                    else:
                        evidence.add(Level.ADVERSARIAL, False,
                                     f"did not terminate cleanly on {case.label}",
                                     "adversary", bindings=bindings)
                if evidence.adversarial_passed == evidence.adversarial_run:
                    evidence.add(Level.ADVERSARIAL, True,
                                 f"survived {evidence.adversarial_run} hostile inputs",
                                 "adversary", bindings=hostile_bindings)

        # -- generalisation ----------------------------------------------------
        held_out = self.adversary.generalisation_cases(spec)
        if held_out:
            ok, results, error = self.tester.run(code, entrypoint, held_out)
            if ok:
                held_bindings: List[Dict[str, Any]] = []
                passed = 0
                for case, result in zip(held_out, results):
                    judged, why = case.judge(
                        bool(result.get("ok")), result.get("value"),
                        str(result.get("error", "")))
                    held_bindings.extend(self._bind_judgment(
                        case, judged, why, result.get("value")))
                    passed += int(judged)
                evidence.generalises = (passed == len(held_out))
                evidence.add(Level.UNIT, evidence.generalises,
                             f"held-out examples: {passed}/{len(held_out)}",
                             "verifier", bindings=held_bindings)
            else:
                evidence.generalises = False
                evidence.add(Level.UNIT, False,
                             f"held-out examples could not run: {error}", "verifier",
                             bindings=self._bind_static(
                                 "tester:execution", Tester.run,
                                 {"code_digest": code_digest,
                                  "kind": "held_out"},
                                 {"ok": False, "error": error[:200]}))

        # -- novelty: does it behave at all on input it has never seen? ------
        probes = self.adversary.novelty_probes(spec)
        if probes:
            ok, results, error = self.tester.run(code, entrypoint, probes)
            if ok and results:
                answered = sum(1 for r in results if r.get("ok"))
                novelty_bindings: List[Dict[str, Any]] = []
                for case, result in zip(probes, results):
                    novelty_bindings.extend(self._bind_judgment(
                        case, bool(result.get("ok")), "novelty probe",
                        result.get("value")))
                if answered == 0:
                    evidence.generalises = False
                    evidence.add(Level.UNIT, False,
                                 "produces no result for any unseen input: this is a "
                                 "memorised table, not an implementation", "adversary",
                                 bindings=novelty_bindings)
                else:
                    evidence.add(Level.UNIT, True,
                                 f"responds on {answered}/{len(probes)} unseen inputs",
                                 "adversary", bindings=novelty_bindings)

        # -- determinism --------------------------------------------------------
        if cases:
            first = [cases[0], cases[0]]
            ok, results, _ = self.tester.run(code, entrypoint, first)
            if ok and len(results) == 2:
                evidence.deterministic = (results[0] == results[1])
                det_bindings: List[Dict[str, Any]] = []
                for result in results:
                    det_bindings.extend(self._bind_judgment(
                        cases[0], True, "determinism repeat",
                        result.get("value")))
                evidence.add(Level.REGRESSION, bool(evidence.deterministic),
                             "same input gives the same answer twice"
                             if evidence.deterministic
                             else "non-deterministic: cannot be regression-tested",
                             "verifier", bindings=det_bindings)
        return evidence


@dataclass
class Verdict:
    admitted: bool
    reasons: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"admitted": self.admitted, "reasons": self.reasons,
                "evidence": self.evidence}


class Arbiter:
    """Decides admission from evidence alone.

    The Arbiter's signature takes `Evidence` and nothing else. There is
    deliberately no parameter through which a generator's claim about its own
    output could be passed.

    ORACLE BINDING (2026-09-25): the old docstring claimed self-certification
    was "unrepresentable" -- an adversarial probe refuted this by getting a
    fully fabricated Evidence admitted. The separation is now ENFORCED: every
    finding at a required level must carry at least one oracle binding, and
    every binding is verified against the tamper-evident registry (the oracle
    evaluated is the oracle registered, over the bound input, producing the
    bound result). Evidence without verified bindings is refused, not scored.
    """

    REQUIRED_LEVELS = (Level.SYNTACTIC, Level.BEHAVIOURAL, Level.ADVERSARIAL)

    def __init__(self, require_generalisation: bool = True,
                 require_determinism: bool = True,
                 oracle_registry: Optional["OracleRegistry"] = None,
                 require_binding: bool = True):
        self.require_generalisation = require_generalisation
        self.require_determinism = require_determinism
        self.oracle_registry = oracle_registry
        # Without a registry the Arbiter cannot verify evidence provenance,
        # so it must refuse rather than score -- fail closed. There is no
        # legacy scoring path: that path is exactly what the fabricated-
        # Evidence probe exploited.
        self.require_binding = require_binding

    def _verify_bindings(self, evidence: Evidence) -> List[str]:
        """Verify every binding on required-level findings. Returns reasons
        (empty = all verified)."""
        reasons: List[str] = []
        reg = self.oracle_registry
        for finding in evidence.findings:
            if finding.level not in self.REQUIRED_LEVELS:
                continue
            if not finding.bindings:
                reasons.append(
                    f"finding at {finding.level.name} level "
                    f"({finding.detail[:80]!r}...) lacks oracle bindings: "
                    f"evidence provenance unverifiable -- refused")
                continue
            for b in finding.bindings:
                ok, why = reg.verify_binding(b, require_head=True)
                if not ok:
                    reasons.append(
                        f"finding at {finding.level.name} level has an "
                        f"unverifiable oracle binding: {why} -- refused")
        return reasons

    def decide(self, evidence: Evidence) -> Verdict:
        reasons: List[str] = []
        if self.require_binding:
            if self.oracle_registry is None:
                return Verdict(
                    admitted=False,
                    reasons=["no oracle registry: evidence provenance "
                             "unverifiable -- refused"],
                    evidence=evidence.as_dict())
            reasons.extend(self._verify_bindings(evidence))
            if reasons:
                # Binding failure is decisive: do not score untrusted
                # evidence on its merits.
                return Verdict(admitted=False, reasons=reasons,
                               evidence=evidence.as_dict())

        levels = evidence.levels_passed()

        for level in self.REQUIRED_LEVELS:
            if levels.get(level.name) is False:
                reasons.append(f"failed {level.name} validation")
            elif level.name not in levels:
                reasons.append(f"no evidence at {level.name} level")

        if evidence.cases_run == 0:
            reasons.append("no behavioural cases were run; nothing was demonstrated")
        elif evidence.cases_passed != evidence.cases_run:
            reasons.append(f"only {evidence.cases_passed}/{evidence.cases_run} "
                           f"behavioural cases passed")

        if self.require_generalisation and evidence.generalises is False:
            reasons.append("fails held-out examples: fitted to the cases it was "
                           "given rather than implementing the behaviour")
        if self.require_determinism and evidence.deterministic is False:
            reasons.append("non-deterministic")

        return Verdict(admitted=not reasons,
                       reasons=reasons or ["all required evidence present"],
                       evidence=evidence.as_dict())


class IndependentValidator:
    """Wires the roles together. Generation happens elsewhere, by design."""

    def __init__(self, runner, arbiter: Optional[Arbiter] = None, seed: int = 0,
                 oracle_registry: Optional["OracleRegistry"] = None,
                 engine_oracle: Optional["EngineOracleHandle"] = None):
        self.verifier = Verifier(Tester(runner), Adversary(seed),
                                 oracle_registry=oracle_registry,
                                 engine_oracle=engine_oracle)
        self.arbiter = arbiter or Arbiter(oracle_registry=oracle_registry)
        # If the caller supplied an arbiter without a registry, binding
        # cannot be verified and decide() will refuse -- fail closed.

    def validate(self, code: str, entrypoint: str, spec,
                 cases: Sequence[Case]) -> Verdict:
        evidence = self.verifier.verify(code, entrypoint, spec, cases)
        return self.arbiter.decide(evidence)
