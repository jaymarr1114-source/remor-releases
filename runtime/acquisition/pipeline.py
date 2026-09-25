"""
swarm_engine/acquisition/pipeline.py

Capability gap detection and governed acquisition.

The planner answers "what can I do with what I have?". This module answers the
other question: "what don't I have that I need, and can I get it safely?"

The pipeline is deliberately paranoid, because acquisition is the one place
where the engine executes code it did not write:

    detect gap -> specify requirement -> discover candidates -> inspect
    -> scan -> resolve dependencies -> sandbox -> test -> compare -> admit

A candidate must clear every stage. It enters at TrustLevel.UNKNOWN and can
only reach TESTED here; TRUSTED is earned later through use, not granted at
the door. Nothing is executed before static scanning passes, and nothing is
admitted with effects the acquiring goal did not ask for — a translation
capability that wants filesystem write is refused even if it works.

Sources are pluggable. A LocalSource (vetted, on-disk) is always available;
network-backed sources exist but stay inert unless the governor grants NETWORK,
so the pipeline is fully testable offline and cannot quietly reach the internet.
"""
from __future__ import annotations

import ast
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from swarm_engine.governance.provenance import Origin, ProvenanceRecord, TrustLevel
from swarm_engine.synthesis.capability_store import stable_code_id
from swarm_engine.primitives.core import Effect, PermissionError_


# ---------------------------------------------------------------------------
# GAP MODEL
# ---------------------------------------------------------------------------

@dataclass
class CapabilityRequirement:
    """A capability the engine needs but may not have."""
    name: str
    description: str = ""
    keywords: List[str] = field(default_factory=list)
    required_effects: List[str] = field(default_factory=list)
    # Bridged fields: value oracles + world postconditions travel with the
    # requirement so admission/verification cannot drop goal obligations.
    examples: Optional[List[Any]] = None
    # O19: content-addressed id of the driver example batch these examples
    # came from (governance/examples_provenance.py). Downstream verdicts
    # cite it so each one names the exact batch it was judged against.
    examples_batch_id: Optional[str] = None
    param_names: Optional[Tuple[str, ...]] = None
    constraints: Dict[str, Any] = field(default_factory=dict)
    postconditions: List[Any] = field(default_factory=list)
    target_domain: Optional[str] = None
    origin: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "keywords": self.keywords,
                "required_effects": self.required_effects,
                "examples": self.examples,
                "examples_batch_id": self.examples_batch_id,
                "param_names": self.param_names,
                "constraints": self.constraints,
                "postconditions": [
                    p.as_dict() if hasattr(p, "as_dict") else p
                    for p in (self.postconditions or [])
                ],
                "target_domain": self.target_domain,
                "origin": self.origin}


@dataclass
class CapabilityGap:
    goal: str
    satisfied: List[str] = field(default_factory=list)
    missing: List[CapabilityRequirement] = field(default_factory=list)
    reason: str = ""

    @property
    def has_gap(self) -> bool:
        return bool(self.missing)

    def as_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "satisfied": self.satisfied,
                "missing": [m.as_dict() for m in self.missing],
                "reason": self.reason, "has_gap": self.has_gap}


class GapDetector:
    """Decides whether a goal is reachable with the current vocabulary.

    The test is behavioural rather than lexical: if the planner can produce a
    plan the composer accepts, there is no gap regardless of what words the
    goal used. Only when planning fails do we ask what the goal was reaching
    for, so the detector never invents a gap for a goal that already works.
    """

    def __init__(self, registry, planner, composer):
        self.reg = registry
        self.planner = planner
        self.composer = composer

    def detect(self, goal: str, allow_effects: bool = False,
               structural_exclusions: Optional[List[Dict[str, Any]]] = None) -> CapabilityGap:
        proposals = self.planner.propose(goal, allow_effects=allow_effects)
        for proposal in proposals:
            # Only a template match counts as evidence the goal is covered.
            # Backward search is a type-directed walk: for "transcribe spoken
            # Japanese audio" it will happily return a plan built from
            # platform_info, because the signature fits and nothing in the type
            # system knows what transcription means. Accepting that as
            # satisfaction is a false negative in exactly the case gap
            # detection exists for -- the engine would conclude it can already
            # do something it cannot, and never acquire the capability.
            if not proposal.strategy.startswith("template"):
                continue
            analysis = self.composer.analyze(proposal.plan)
            if analysis.ok:
                return CapabilityGap(goal=goal, satisfied=proposal.ops_used,
                                     reason="an executable plan already exists")

        missing = self._infer_requirements(goal)
        if structural_exclusions:
            # Only promote hard constructibility holes (e.g. CALLABLE with no
            # producers), not every empty bank slot for producible types.
            hard = [e for e in structural_exclusions
                    if "callable" in str(e.get("required_type", "")).lower()]
            structural_exclusions = hard
        if structural_exclusions:
            # Promote structural/search-space gap ahead of shallow unclassified.
            structural = CapabilityRequirement(
                name="structural_unconstructible_argument",
                description=(
                    "search reported required argument type(s) with no "
                    "constructible producer in the current expression grammar: "
                    f"{structural_exclusions[:5]}"
                ),
                keywords=["structural", "constructibility", "search-space"],
                required_effects=[],
                constraints={"unsatisfiable_args": list(structural_exclusions)[:20]},
            )
            # Replace pure unclassified-only lists; keep other catalogue hits.
            missing = [m for m in missing if m.name != "unclassified"]
            missing.insert(0, structural)
        return CapabilityGap(
            goal=goal, missing=missing,
            reason=(f"no plan over the {len(self.reg)} available primitives "
                    f"satisfies {goal!r}"
                    + ("; structural exclusion: unconstructible argument type"
                       if structural_exclusions else "")))

    def _infer_requirements(self, goal: str) -> List[CapabilityRequirement]:
        """Name what the goal appears to need that the registry lacks.

        Combines the historical keyword catalogue with the declarative
        effect lexicon (intent.infer_required_effects) so effectful goals
        such as filesystem writes are not classified as pure unclassified.
        """
        from swarm_engine.acquisition.intent import (
            infer_required_effects, infer_postconditions,
        )
        lowered = goal.lower()
        lexicon_effects = infer_required_effects(goal)
        posts = infer_postconditions(goal)
        catalogue = [
            ("speech_recognition", ("speech", "transcribe", "spoken", "audio to text"),
             ("network",)),
            ("translation", ("translate", "translation", "japanese", "spanish",
                             "french", "german"), ("network",)),
            ("image_understanding", ("image", "photo", "picture", "detect object",
                                     "ocr"), ("read_fs",)),
            ("web_retrieval", ("web", "internet", "download", "scrape", "url"),
             ("network",)),
            ("pdf_extraction", ("pdf", "document extract"), ("read_fs",)),
            ("statistics", ("regression", "correlation", "significance"), ()),
            ("filesystem_write", ("write", "save", "create file", "overwrite"),
             ("write_fs",)),
            ("filesystem_read", ("read file", "load file", "open file"),
             ("read_fs",)),
        ]
        found: List[CapabilityRequirement] = []
        for name, keywords, effects in catalogue:
            if not any(k in lowered for k in keywords):
                continue
            if self._registry_covers(name, keywords):
                continue
            merged = list(effects)
            for e in lexicon_effects:
                if e not in merged:
                    merged.append(e)
            found.append(CapabilityRequirement(
                name=name, description=goal,
                keywords=[k for k in keywords if k in lowered] or list(keywords),
                required_effects=merged,
                postconditions=list(posts) if "write_fs" in merged or "read_fs" in merged else []))

        if not found:
            # The identity that matters here is per-DESCRIPTION, not a
            # shared category label. Found necessary directly: the literal
            # string "unclassified" was being reused as the .name field
            # for every goal that didn't match a known keyword category,
            # and that name later became the REGISTRATION name for a
            # GENERATE-acquired primitive — meaning the first such
            # acquisition in an engine's lifetime silently claimed the
            # identity for every subsequent, unrelated goal that also
            # fell through to this branch. "unclassified" remains a
            # legitimate CATEGORY word (used nowhere as identity now); the
            # identity-bearing .name is a slug of the goal's own text, so
            # two different descriptions can never collide, and the same
            # description deterministically maps back to the same name
            # (which is exactly what makes prior-acquisition reuse work).
            slug_chars = [c if (c.isalnum()) else "_" for c in lowered]
            slug = "".join(slug_chars).strip("_")
            while "__" in slug:
                slug = slug.replace("__", "_")
            slug = slug[:60] or "unclassified"
            found.append(CapabilityRequirement(
                name=slug,
                # The description is the GOAL, not a diagnostic. A
                # diagnostic message here ("no template matched...")
                # poisons downstream synthesis: the orchestrator's
                # propose_multi fallback searches for the description
                # text, and a diagnostic string is not a synthesizable
                # goal. Diagnostics belong in evidence/logs.
                description=goal,
                keywords=[w for w in lowered.split() if len(w) > 3][:6],
                required_effects=list(lexicon_effects),
                postconditions=list(posts)))
        return found

    def _registry_covers(self, name: str, keywords: Tuple[str, ...]) -> bool:
        if name in self.reg:
            return True
        return any(self.reg.search(k) for k in keywords)


# ---------------------------------------------------------------------------
# CANDIDATES AND SOURCES
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """A proposed implementation of a missing capability."""
    name: str
    source: str                       # where it came from
    code: str = ""                    # python source defining `capability(...)`
    entrypoint: str = "capability"
    declared_effects: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    license: str = "unknown"
    notes: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "source": self.source,
                "entrypoint": self.entrypoint,
                "declared_effects": self.declared_effects,
                "dependencies": self.dependencies, "license": self.license}


class CandidateSource(ABC):
    """Somewhere candidates can be obtained from."""
    requires_effect: Optional[Effect] = None

    @abstractmethod
    def search(self, requirement: CapabilityRequirement) -> List[Candidate]:
        ...


class LocalSource(CandidateSource):
    """Vetted, in-process candidates. Always available: no effect required,
    nothing leaves the machine."""

    def __init__(self, catalogue: Optional[Dict[str, Candidate]] = None):
        self.catalogue = catalogue or {}

    def offer(self, requirement_name: str, candidate: Candidate) -> None:
        self.catalogue[requirement_name] = candidate

    def search(self, requirement: CapabilityRequirement) -> List[Candidate]:
        hit = self.catalogue.get(requirement.name)
        return [hit] if hit else []


class NetworkSource(CandidateSource):
    """A remote index. Inert unless NETWORK is granted.

    It asks the governor rather than trying and catching, so a denied search
    is an auditable governance event rather than a swallowed exception.
    """
    requires_effect = Effect.NETWORK

    def __init__(self, governor, fetcher: Optional[Callable[[str], List[Candidate]]] = None,
                 endpoint: str = "https://example.invalid/index"):
        self.governor = governor
        self.fetcher = fetcher
        self.endpoint = endpoint

    def search(self, requirement: CapabilityRequirement) -> List[Candidate]:
        allowed, why = self.governor.allows(Effect.NETWORK, self.endpoint)
        if not allowed:
            raise PermissionError_(f"candidate search denied: {why}")
        if self.fetcher is None:
            return []
        return self.fetcher(requirement.name)



def http_json_index_fetcher(endpoint: str, timeout: float = 8.0):
    """Generic NetworkSource.fetcher: GET a JSON candidate index.

    Protocol (generic, no site-specific knowledge):
      GET {endpoint}?q={requirement_name}   (fallback: GET endpoint)
      Response JSON: list of objects OR {"candidates": [...] } where each
      object has at least {"name", "code"} and optional entrypoint /
      declared_effects / dependencies / license / notes.

    Returns Candidate list. Network errors raise; empty/malformed JSON
    yields []. The governor still gates NETWORK before this runs.
    """
    import json
    import urllib.parse
    import urllib.request

    def _fetch(requirement_name: str):
        from swarm_engine.acquisition.pipeline import Candidate
        q = urllib.parse.urlencode({"q": requirement_name})
        url = endpoint
        sep = "&" if ("?" in endpoint) else "?"
        urls = [f"{endpoint}{sep}{q}", endpoint]
        last_err = None
        body = None
        used = None
        for u in urls:
            try:
                req = urllib.request.Request(
                    u, headers={"Accept": "application/json",
                                "User-Agent": "REMOR-NetworkSource/1"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read()
                    used = u
                    break
            except Exception as exc:
                last_err = exc
                continue
        if body is None:
            raise RuntimeError(
                f"network index fetch failed for {requirement_name!r} "
                f"via {endpoint!r}: {last_err}")
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except Exception as exc:
            raise RuntimeError(f"network index returned non-JSON from {used}: {exc}")
        if isinstance(data, dict):
            data = data.get("candidates") or data.get("results") or []
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            code = item.get("code") or item.get("source_code") or ""
            name = item.get("name") or requirement_name
            if not code:
                continue
            out.append(Candidate(
                name=str(name),
                source=f"network:{used}",
                code=str(code),
                entrypoint=str(item.get("entrypoint") or "capability"),
                declared_effects=list(item.get("declared_effects") or []),
                dependencies=list(item.get("dependencies") or []),
                license=str(item.get("license") or "unknown"),
                notes=str(item.get("notes") or f"fetched for {requirement_name}"),
            ))
        return out

    return _fetch


# ---------------------------------------------------------------------------
# INSPECTION AND SANDBOXING
# ---------------------------------------------------------------------------

# Imports that grant a candidate reach beyond its declared effects. This list is
# a floor, not a security boundary: it stops careless code, not a determined
# adversary. Real isolation needs a separate process with OS-level limits, and
# the docstring says so rather than implying the AST check is sufficient.
DANGEROUS_IMPORTS = {
    "os": Effect.PROCESS, "sys": Effect.MUTATE_SELF, "subprocess": Effect.PROCESS,
    "socket": Effect.NETWORK, "urllib": Effect.NETWORK, "requests": Effect.NETWORK,
    "http": Effect.NETWORK, "shutil": Effect.WRITE_FS, "pathlib": Effect.READ_FS,
    "ctypes": Effect.PROCESS, "importlib": Effect.MUTATE_SELF,
    "builtins": Effect.MUTATE_SELF, "pickle": Effect.PROCESS,
}

FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "globals", "locals",
                   "setattr", "delattr", "open"}


@dataclass
class ScanResult:
    passed: bool
    inferred_effects: List[str] = field(default_factory=list)
    undeclared_effects: List[str] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"passed": self.passed, "inferred_effects": self.inferred_effects,
                "undeclared_effects": self.undeclared_effects,
                "violations": self.violations}


class CandidateScanner:
    """Static analysis of candidate source before it is ever executed."""

    def scan(self, candidate: Candidate) -> ScanResult:
        try:
            tree = ast.parse(candidate.code)
        except SyntaxError as exc:
            return ScanResult(False, violations=[f"does not parse: {exc}"])

        violations: List[str] = []
        inferred: set = set()
        found_entrypoint = False

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""])
                for module in names:
                    root = module.split(".")[0]
                    if root in DANGEROUS_IMPORTS:
                        inferred.add(DANGEROUS_IMPORTS[root].value)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in FORBIDDEN_CALLS:
                    violations.append(f"calls {node.func.id}()")
            elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                violations.append(f"reaches for dunder attribute {node.attr!r}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == candidate.entrypoint:
                    found_entrypoint = True

        if not found_entrypoint:
            violations.append(f"defines no entrypoint {candidate.entrypoint!r}")

        declared = set(candidate.declared_effects)
        undeclared = sorted(inferred - declared)
        if undeclared:
            # An effect the candidate did not declare is the important finding:
            # governance can only gate what it was told about.
            violations.append(f"uses undeclared effects: {', '.join(undeclared)}")

        return ScanResult(passed=not violations, inferred_effects=sorted(inferred),
                          undeclared_effects=undeclared, violations=violations)


class Sandbox:
    """Executes a scanned candidate with a restricted namespace and limits.

    This is in-process isolation: a restricted __builtins__, no module access
    beyond an allowlist, a wall-clock cap and an output size cap. It is
    adequate for code that has already passed scanning and is honest about
    what it is not — it will not contain code that is actively hostile. Running
    genuinely untrusted code needs a subprocess with rlimits or a container,
    and the pipeline refuses to promote past SANDBOXED without that.
    """

    SAFE_BUILTINS = {
        "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
        "divmod": divmod, "enumerate": enumerate, "filter": filter, "float": float,
        "int": int, "isinstance": isinstance, "len": len, "list": list, "map": map,
        "max": max, "min": min, "pow": pow, "range": range, "repr": repr,
        "reversed": reversed, "round": round, "set": set, "sorted": sorted,
        "str": str, "sum": sum, "tuple": tuple, "zip": zip,
        "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,
        "IndexError": IndexError, "ZeroDivisionError": ZeroDivisionError,
    }
    ALLOWED_MODULES = {"math", "json", "re", "statistics", "datetime", "itertools",
                       "functools", "collections"}

    def __init__(self, timeout_s: float = 2.0, max_output_bytes: int = 200_000):
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes

    def load(self, candidate: Candidate) -> Tuple[bool, Any, str]:
        """Compile the candidate and return its entrypoint."""
        def guarded_import(name, *args, **kwargs):
            root = name.split(".")[0]
            if root not in self.ALLOWED_MODULES:
                raise ImportError(f"sandbox forbids importing {name!r}")
            return __import__(name, *args, **kwargs)

        namespace: Dict[str, Any] = {
            "__builtins__": {**self.SAFE_BUILTINS, "__import__": guarded_import},
        }
        try:
            exec(compile(candidate.code, f"<candidate:{candidate.name}>", "exec"),
                 namespace)
        except Exception as exc:
            return False, None, f"failed to load: {type(exc).__name__}: {exc}"

        fn = namespace.get(candidate.entrypoint)
        if not callable(fn):
            return False, None, f"entrypoint {candidate.entrypoint!r} is not callable"
        return True, fn, ""

    def call(self, fn: Callable, args: Dict[str, Any]) -> Tuple[bool, Any, str]:
        started = time.time()
        try:
            value = fn(**args)
        except Exception as exc:
            return False, None, f"{type(exc).__name__}: {exc}"
        elapsed = time.time() - started
        if elapsed > self.timeout_s:
            return False, None, f"exceeded time budget ({elapsed:.2f}s > {self.timeout_s}s)"
        try:
            if len(repr(value)) > self.max_output_bytes:
                return False, None, "output exceeded size budget"
        except Exception:
            pass
        return True, value, ""


# ---------------------------------------------------------------------------
# PIPELINE
# ---------------------------------------------------------------------------

@dataclass
class AcquisitionResult:
    accepted: bool
    requirement: str
    candidate: Optional[Candidate] = None
    trust: TrustLevel = TrustLevel.UNKNOWN
    stage: str = ""
    reasons: List[str] = field(default_factory=list)
    scan: Optional[ScanResult] = None
    tests_passed: int = 0
    tests_total: int = 0
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    capability_id: str = ""
    registered: bool = False
    isolation: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted, "requirement": self.requirement,
            "candidate": self.candidate.as_dict() if self.candidate else None,
            "trust": self.trust.name, "stage": self.stage, "reasons": self.reasons,
            "scan": self.scan.as_dict() if self.scan else None,
            "tests": f"{self.tests_passed}/{self.tests_total}",
            "rejected": self.rejected, "capability_id": self.capability_id,
            "registered": self.registered, "isolation": self.isolation,
            "evidence": self.evidence,
        }


@dataclass
class AcquisitionTest:
    args: Dict[str, Any]
    expect: Any = None
    predicate: Optional[Callable[[Any], bool]] = None
    # O12: channel binding for the predicate oracle. A predicate-carrying
    # test judged while a registry is present must name its registered
    # oracle here (see AcquisitionPipeline.bind_predicate); otherwise the
    # pipeline test gate refuses it outright.
    oracle_id: Optional[str] = None
    oracle_version: Optional[int] = None

    def judge(self, value: Any) -> Tuple[bool, str]:
        if self.predicate is not None:
            try:
                return bool(self.predicate(value)), "predicate"
            except Exception as exc:
                return False, f"predicate raised: {exc}"
        if self.expect is not None:
            return value == self.expect, f"expected {self.expect!r}, got {value!r}"
        return value is not None, "expected a non-null result"


class AcquisitionPipeline:
    """Runs a requirement through discovery, scanning, sandboxing and testing."""

    def __init__(self, sources: List[CandidateSource], provenance,
                 scanner: Optional[CandidateScanner] = None,
                 sandbox: Optional[Sandbox] = None,
                 process_sandbox=None, matcher=None, registrar=None,
                 available_dependencies: Optional[List[str]] = None,
                 oracle_registry=None, engine_oracle=None):
        self.sources = sources
        self.provenance = provenance
        self.scanner = scanner or CandidateScanner()
        self.sandbox = sandbox or Sandbox()
        # O12: when a registry is present, the test gate below refuses
        # unregistered predicates and records (test, candidate, input,
        # verdict) as chained evaluations. When absent, legacy judge
        # behavior is preserved exactly (classified as unbound).
        self.oracle_registry = oracle_registry
        self.engine_oracle = engine_oracle
        # Process isolation is layered *around* the in-process sandbox, never
        # instead of it: scanning and the restricted namespace remain the first
        # filters, and the subprocess is the boundary underneath them.
        if process_sandbox is None:
            from swarm_engine.acquisition.isolation import ProcessSandbox
            process_sandbox = ProcessSandbox()
        self.process_sandbox = process_sandbox
        self.matcher = matcher
        # `registrar(name, candidate, capability_id) -> bool` makes an admitted
        # capability visible to the planner. Without it acquisition would end
        # at "we have the code", which is not the same as "the engine can use
        # it on the next objective".
        self.registrar = registrar
        self.available_dependencies = set(
            available_dependencies if available_dependencies is not None
            else Sandbox.ALLOWED_MODULES)

    def resolve_dependencies(self, candidate: Candidate) -> Tuple[bool, List[str]]:
        """Every declared dependency must already be satisfiable in the
        sandbox. Acquisition does not install packages: fetching and executing
        arbitrary third-party code to satisfy a dependency would reopen, one
        level down, exactly the hole the scanner and sandbox exist to close."""
        missing = [d for d in candidate.dependencies
                   if d.split(".")[0] not in self.available_dependencies]
        return (not missing), missing

    # -- O12 predicate-oracle binding --------------------------------------
    def bind_predicate(self, name: str, predicate: Callable[[Any], bool],
                       producer_id: str, token: str) -> Tuple[str, int]:
        """Register a test predicate under an authenticated producer.

        Returns (oracle_id, version) to stamp onto
        ``AcquisitionTest(..., predicate=predicate, oracle_id=..., ...)``.
        Requires the pipeline to have an oracle registry; without one the
        predicate cannot be bound and this raises.
        """
        from swarm_engine.governance.oracle_binding import OracleBindingError
        if self.oracle_registry is None:
            raise OracleBindingError(
                "bind_predicate: this pipeline has no oracle registry -- "
                "the predicate cannot be bound (legacy unbound judge applies)")
        if not self.oracle_registry.authenticate(producer_id, token):
            raise OracleBindingError(
                "bind_predicate: producer authentication failed -- forged "
                "or missing identity refused")
        return self.oracle_registry.register_oracle(
            producer_id, token, name, predicate,
            input_contract="candidate output value",
            output_contract="judge(value) -> bool",
            source="acquisition pipeline test gate")

    def _judge_test(self, test: AcquisitionTest, value: Any,
                    candidate, test_index: int) -> Tuple[bool, str]:
        """The pipeline test gate. In the bound configuration a
        predicate-carrying test must name its registered oracle; the live
        predicate is re-digested against the registration (O1 pattern) and
        the verdict is recorded as a chained (test, candidate, input,
        verdict) evaluation. Unregistered predicates are refused outright.
        Without a registry this is exactly the legacy ``test.judge``.
        """
        if self.oracle_registry is None or test.predicate is None:
            return test.judge(value)
        from swarm_engine.governance.binding_helpers import (
            verify_live_callable)
        from swarm_engine.governance.oracle_binding import OracleBindingError
        if test.oracle_id is None:
            raise OracleBindingError(
                "pipeline test gate: unregistered predicate refused -- a "
                "predicate may judge a candidate only as a registered "
                "oracle (see AcquisitionPipeline.bind_predicate)")
        version = test.oracle_version
        if version is None:
            head = self.oracle_registry.oracle_head(test.oracle_id)
            if head is None:
                raise OracleBindingError(
                    "pipeline test gate: unregistered predicate refused -- "
                    f"no oracle {test.oracle_id!r} is registered")
            version = head["version"]
        row = verify_live_callable(
            self.oracle_registry, test.oracle_id, version,
            test.predicate, what="acquisition test predicate")
        judged, why = test.judge(value)
        if self.engine_oracle is not None:
            from swarm_engine.governance.binding_helpers import (
                canonical_digest)
            self.engine_oracle.evaluate(
                test.oracle_id,
                {"test_index": test_index, "test_args": test.args,
                 "candidate_digest": canonical_digest(candidate.code),
                 "value": value},
                {"passed": judged, "why": why[:400]},
                input_ref=f"acquisition_test:{test_index}",
                version=version, supplier_id=row["producer_id"])
        return judged, why

    def acquire(self, requirement: CapabilityRequirement,
                tests: Optional[List[AcquisitionTest]] = None,
                parents: Optional[List[str]] = None,
                cases: Optional[List[Any]] = None,
                criteria: Optional[Any] = None) -> AcquisitionResult:
        result = AcquisitionResult(accepted=False, requirement=requirement.name)

        candidates: List[Candidate] = []
        for source in self.sources:
            try:
                candidates.extend(source.search(requirement))
            except PermissionError_ as exc:
                result.reasons.append(str(exc))
            except Exception as exc:
                result.reasons.append(f"source {type(source).__name__} failed: {exc}")

        if not candidates:
            result.stage = "discovery"
            result.reasons.append(f"no candidate found for {requirement.name!r}")
            return result

        for candidate in candidates:
            verdict = self._evaluate(candidate, requirement, tests, parents or [],
                                     cases, criteria)
            if verdict.accepted:
                return verdict
            result.rejected.append({"candidate": candidate.name,
                                    "stage": verdict.stage,
                                    "reasons": verdict.reasons})

        result.stage = "evaluation"
        result.reasons.append(f"{len(candidates)} candidate(s) found, none acceptable")
        return result

    def _evaluate(self, candidate: Candidate, requirement: CapabilityRequirement,
                  tests: Optional[List[AcquisitionTest]],
                  parents: List[str], cases=None,
                  criteria=None) -> AcquisitionResult:
        result = AcquisitionResult(accepted=False, requirement=requirement.name,
                                   candidate=candidate)

        # -- effect budget: never acquire more reach than the goal asked for --
        excess = set(candidate.declared_effects) - set(requirement.required_effects)
        if excess:
            result.stage = "effect_budget"
            result.reasons.append(
                f"candidate demands effects the requirement does not justify: "
                f"{', '.join(sorted(excess))}")
            return result

        # -- effect coverage: required effects must be covered by the candidate
        # (#4). A pure candidate cannot satisfy a write_fs (etc.) requirement
        # even if its return value matches an example oracle (#9).
        required = set(requirement.required_effects or [])
        declared = set(candidate.declared_effects or [])
        # Static scan may infer effects from imports; prefer the richer set.
        inferred = set()
        try:
            pre_scan = self.scanner.scan(candidate)
            inferred = set(getattr(pre_scan, "inferred_effects", None) or [])
        except Exception:
            pass
        covered = declared | inferred
        missing_effects = required - covered
        if missing_effects:
            result.stage = "effect_coverage"
            result.reasons.append(
                f"candidate does not cover required effects: "
                f"{', '.join(sorted(missing_effects))}")
            return result

        # -- static scan, before anything executes ---------------------------
        scan = self.scanner.scan(candidate)
        result.scan = scan
        if not scan.passed:
            result.stage = "scan"
            result.reasons.extend(scan.violations)
            return result
        result.trust = TrustLevel.SCANNED

        # -- dependencies -----------------------------------------------------
        resolved, missing = self.resolve_dependencies(candidate)
        if not resolved:
            result.stage = "dependencies"
            result.reasons.append(
                f"unsatisfiable dependencies in the sandbox: {missing}")
            return result

        # -- semantic suitability ---------------------------------------------
        if self.matcher is not None and criteria is not None:
            match = self.matcher.score(
                requirement.description or requirement.name,
                description=f"{candidate.name} {candidate.notes}",
                declared_effects=candidate.declared_effects,
                criteria=criteria)
            result.reasons.extend(match.reasons)
            if not match.sufficient:
                result.stage = "semantic"
                result.reasons.extend(match.against)
                return result

        # -- in-process load (first filter) ------------------------------------
        loaded, fn, error = self.sandbox.load(candidate)
        if not loaded:
            result.stage = "sandbox"
            result.reasons.append(error)
            return result
        result.trust = TrustLevel.SANDBOXED

        # -- behavioural verification under process isolation -------------------
        suite = tests or []
        result.tests_total = len(suite)
        if suite:
            calls = [dict(t.args) for t in suite]
            report = self.process_sandbox.run(candidate.code, candidate.entrypoint,
                                              calls)
            if not report.ok:
                # Fall back to the in-process sandbox only where isolation is
                # genuinely unavailable on this platform -- never to route
                # around a limit the subprocess actually enforced.
                if "unavailable on this platform" in report.error:
                    result.reasons.append(
                        "process isolation unavailable; verified in-process only")
                    for test_index, test in enumerate(suite):
                        ok, value, err = self.sandbox.call(fn, test.args)
                        if not ok:
                            result.stage = "test"
                            result.reasons.append(f"raised on {test.args}: {err}")
                            return result
                        judged, why = self._judge_test(
                            test, value, candidate, test_index)
                        if not judged:
                            result.stage = "test"
                            result.reasons.append(f"wrong result for {test.args}: {why}")
                            return result
                        result.tests_passed += 1
                else:
                    result.stage = "isolation"
                    result.reasons.append(report.error)
                    result.isolation = report.as_dict()
                    return result
            else:
                result.isolation = report.as_dict()
                for test_index, (test, outcome) in enumerate(
                        zip(suite, report.value or [])):
                    if not outcome.get("ok"):
                        result.stage = "test"
                        result.reasons.append(
                            f"raised on {test.args}: {outcome.get('error','')[:120]}")
                        return result
                    judged, why = self._judge_test(
                        test, outcome.get("value"), candidate, test_index)
                    if not judged:
                        result.stage = "test"
                        result.reasons.append(f"wrong result for {test.args}: {why}")
                        return result
                    result.tests_passed += 1

        # -- semantic behaviour suite -------------------------------------------
        if cases:
            from swarm_engine.acquisition.semantic import SemanticVerifier
            evidence = SemanticVerifier(self.process_sandbox.run).verify(
                candidate.code, candidate.entrypoint, cases,
                candidate.declared_effects)
            result.evidence = evidence.as_dict()
            if not evidence.passed:
                result.stage = "semantic_verification"
                result.reasons.extend(evidence.failures[:5])
                return result

        if suite or cases:
            result.trust = TrustLevel.TESTED
        else:
            # Untested code does not get to claim it was tested. It stays at
            # SANDBOXED, which the engine treats as not-yet-usable in
            # production, rather than being quietly waved through.
            result.reasons.append("no tests supplied; capped at SANDBOXED")

        capability_id = stable_code_id("acq", requirement.name, candidate.code)
        self.provenance.record(ProvenanceRecord(
            capability_id=capability_id, origin=Origin.ACQUIRED,
            trust=result.trust, source=candidate.source, parents=parents,
            effects=list(candidate.declared_effects)))
        self.provenance.log(capability_id, "acquired",
                            f"{result.tests_passed}/{result.tests_total} tests, "
                            f"trust={result.trust.name}")
        if result.evidence:
            self.provenance.log(capability_id, "verified", str(result.evidence)[:400])

        # -- make it usable ------------------------------------------------------
        if self.registrar is not None and result.trust >= TrustLevel.TESTED:
            try:
                registered = self.registrar(requirement.name, candidate, capability_id)
                result.registered = bool(registered)
                self.provenance.log(capability_id, "registered",
                                    f"available to the planner as {requirement.name!r}")
            except Exception as exc:
                result.reasons.append(f"registration failed: {exc}")

        result.accepted = True
        result.stage = "admitted"
        result.capability_id = capability_id
        candidate.notes = capability_id
        return result
