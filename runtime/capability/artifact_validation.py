"""
swarm_engine/capability/artifact_validation.py

Validators for artifacts generated from externally-researched knowledge —
code in a language SWarm has no primitive vocabulary for, so composition
can't produce or check it, but structural correctness can still be
verified honestly.

Extensible by registry (`register_validator`), not by editing the growth
engine's control flow — a new target_domain gets a new validator entry, not
a new branch in growth_engine.py.

Explicit scope limit: these are STRUCTURAL/lint-level checks, not a real
parser and nowhere close to a real interpreter. GDScriptStructuralValidator's
rules are derived directly from fetched documentation and catch a genuine,
useful subset of malformed code, but passing this validator is not
equivalent to Godot's own parser accepting the file. Runtime behavior is
not checked at all; Godot is not installed in this environment.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ValidationVerdict:
    valid: bool
    checks_passed: List[str] = field(default_factory=list)
    checks_failed: List[str] = field(default_factory=list)
    scope_note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"valid": self.valid, "checks_passed": self.checks_passed,
                "checks_failed": self.checks_failed, "scope_note": self.scope_note}


class TextArtifactValidator(ABC):
    domain: str = ""

    @abstractmethod
    def validate(self, content: str) -> ValidationVerdict:
        ...


class GDScriptStructuralValidator(TextArtifactValidator):
    """Rules sourced directly from fetched documentation
    (docs.godotengine.org GDScript reference and corroborating tutorials,
    retrieved this session):
      - indentation-based blocks, like Python
      - no "else if" — GDScript requires `elif`
      - `func` declarations take the form `func name(args) -> type:`
    """
    domain = "gdscript"

    def validate(self, content: str) -> ValidationVerdict:
        passed: List[str] = []
        failed: List[str] = []

        if content.count("(") == content.count(")"):
            passed.append("balanced parentheses")
        else:
            failed.append("unbalanced parentheses")

        if content.count("[") == content.count("]"):
            passed.append("balanced brackets")
        else:
            failed.append("unbalanced brackets")

        if "else if" in content:
            failed.append("uses 'else if', which GDScript does not support "
                          "— use 'elif' (confirmed via fetched documentation)")
        else:
            passed.append("no invalid 'else if' usage")

        lines = content.split("\n")
        indent_failures = self._check_indentation(lines)
        if indent_failures:
            failed.append(f"inconsistent indentation at line(s) {indent_failures}")
        else:
            passed.append("consistent indentation")

        func_failures = self._check_func_declarations(lines)
        if func_failures:
            failed.append(f"malformed func declaration at line(s) {func_failures}")
        else:
            passed.append("func declarations well-formed")

        return ValidationVerdict(
            valid=not failed, checks_passed=passed, checks_failed=failed,
            scope_note=("structural/lint-level checks derived from fetched "
                       "documentation — NOT a real GDScript parser and NOT "
                       "equivalent to Godot accepting this file; Godot is "
                       "not installed in this environment and runtime "
                       "behavior is not checked"))

    def _check_indentation(self, lines: List[str]) -> List[int]:
        bad = []
        indent_unit = None
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip(" ")
            if not stripped or stripped.startswith("#"):
                continue
            leading = len(line) - len(stripped)
            if "\t" in line[:leading] and " " in line[:leading]:
                bad.append(i)
                continue
            if leading > 0 and indent_unit is None:
                indent_unit = leading
            if indent_unit and leading % indent_unit != 0 and "\t" not in line[:leading]:
                bad.append(i)
        return bad

    def _check_func_declarations(self, lines: List[str]) -> List[int]:
        bad = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("func "):
                if "(" not in stripped or ")" not in stripped or not stripped.endswith(":"):
                    bad.append(i)
        return bad


class PythonAPIValidator(TextArtifactValidator):
    """Unlike GDScriptStructuralValidator, this uses Python's own real
    parser (ast.parse) rather than heuristic structural checks — Python is
    actually available in this environment, so 'valid syntax' can be a
    genuine guarantee here, not an approximation. Still does not execute
    the code or verify a specific library's API contract, so a
    syntactically valid script calling a nonexistent function would still
    pass that part; that gap is real and stated, not hidden."""
    domain = "python"

    def __init__(self, required_import: Optional[str] = None):
        self.required_import = required_import

    def validate(self, content: str) -> ValidationVerdict:
        import ast
        passed: List[str] = []
        failed: List[str] = []

        try:
            tree = ast.parse(content)
            passed.append("valid Python syntax (verified via ast.parse, "
                          "Python's real parser)")
        except SyntaxError as exc:
            failed.append(f"invalid Python syntax: {exc}")
            return ValidationVerdict(
                valid=False, checks_passed=passed, checks_failed=failed,
                scope_note="ast.parse failed; nothing further checked")

        if self.required_import:
            imports_module = any(
                (isinstance(node, ast.Import) and
                 any(a.name == self.required_import for a in node.names))
                or (isinstance(node, ast.ImportFrom) and
                    node.module == self.required_import)
                for node in ast.walk(tree))
            if imports_module:
                passed.append(f"imports {self.required_import}")
            else:
                failed.append(f"does not import {self.required_import}")

        return ValidationVerdict(
            valid=not failed, checks_passed=passed, checks_failed=failed,
            scope_note=("real Python syntax validation via ast.parse" +
                       (f", plus an import check for {self.required_import}"
                        if self.required_import else "") +
                       " — NOT a check that any specific library API calls "
                       "used actually exist or behave as expected; the "
                       "script is never executed by this validator itself "
                       "(a separate execution-level check exists at the "
                       "project level in coordinated_growth.py)"))


_REGISTRY: Dict[str, TextArtifactValidator] = {}

# -- O18 oracle binding (trust-anchor mission, workstream 4) -----------------
# The registry used to be a silent last-write-wins overwrite with no
# authentication: any module could neuter artifact validation for a domain
# invisibly. When oracle binding is active (see bind_oracle_registry, called
# at engine boot), registration requires an authenticated producer, every
# registration is a chained record (domain, validator definition digest,
# producer, timestamp), and the use site verifies the live validator's bytes
# against the registered head before trusting its verdict. When no binding
# is active, the legacy behavior below is preserved exactly (unbound).
_ORACLE_REGISTRY = None        # OracleRegistry, once bound
_ENGINE_ORACLE = None          # EngineOracleHandle, once bound
# domain -> {"oracle_id":..., "version":..., "producer_id":...}
_REGISTRY_BINDINGS: Dict[str, Dict[str, Any]] = {}


def _validator_definition(validator: TextArtifactValidator) -> Dict[str, Any]:
    """Canonical definition of a validator: its class source plus instance
    configuration. A swapped validator (different class or different config)
    has a different digest -- this is what the use site re-checks."""
    import inspect
    cls = type(validator)
    try:
        source = inspect.getsource(cls)
    except (OSError, TypeError):
        source = None
    try:
        config = dict(vars(validator))
    except TypeError:
        config = {}
    return {"kind": "artifact_validator",
            "class": cls.__module__ + "." + cls.__name__,
            "domain": validator.domain,
            "source": source, "config": config}


def bind_oracle_registry(registry, engine_handle) -> None:
    """Activate O18 binding. Pins the engine builtins as engine-authorized
    definitions; every later register_validator call requires an
    authenticated producer. Idempotent (re-binding re-pins without churn:
    register_oracle returns the head version for identical definitions)."""
    global _ORACLE_REGISTRY, _ENGINE_ORACLE
    _ORACLE_REGISTRY = registry
    _ENGINE_ORACLE = engine_handle
    # Pin the engine builtins as engine-authorized definitions -- and ONLY
    # the builtins (identified by class, not by presence in the registry, so
    # a third-party validator registered pre-bind is never mislabeled as
    # engine-authorized; it must re-register with its own credential).
    _BUILTIN_CLASSES = {"GDScriptStructuralValidator", "PythonAPIValidator"}
    for domain, validator in list(_REGISTRY.items()):
        if type(validator).__name__ not in _BUILTIN_CLASSES:
            continue
        oracle_id, version = engine_handle.register_oracle(
            "artifact_validator:" + domain,
            _validator_definition(validator),
            input_contract="artifact text",
            output_contract="ValidationVerdict",
            source="engine bootstrap: builtin artifact validator "
                   "(engine-authorized)")
        _REGISTRY_BINDINGS[domain] = {
            "oracle_id": oracle_id, "version": version,
            "producer_id": engine_handle.producer_id}


def _binding_active() -> bool:
    return _ORACLE_REGISTRY is not None and _ENGINE_ORACLE is not None


def register_validator(validator: TextArtifactValidator,
                       producer_id: Optional[str] = None,
                       token: Optional[str] = None) -> None:
    """Register (or overwrite) the validator for ``validator.domain``.

    Unbound configuration: legacy silent last-write-wins, preserved exactly.
    Bound configuration: requires an authenticated producer (a missing or
    forged credential raises ``OracleBindingError`` and the registry is
    untouched); the registration is recorded as a chained oracle row and the
    domain's binding head moves to the new registration.
    """
    if _binding_active():
        if producer_id is None or token is None:
            from swarm_engine.governance.oracle_binding import (
                OracleBindingError)
            raise OracleBindingError(
                "register_validator: authenticated producer required while "
                "oracle binding is active -- refusing an unattributed "
                "validator registration")
        if not _ORACLE_REGISTRY.authenticate(producer_id, token):
            from swarm_engine.governance.oracle_binding import (
                OracleBindingError)
            raise OracleBindingError(
                "register_validator: producer authentication failed -- "
                "forged or missing identity refused")
        oracle_id, version = _ORACLE_REGISTRY.register_oracle(
            producer_id, token,
            "artifact_validator:" + validator.domain,
            _validator_definition(validator),
            input_contract="artifact text",
            output_contract="ValidationVerdict",
            source="artifact_validation.register_validator")
        _REGISTRY_BINDINGS[validator.domain] = {
            "oracle_id": oracle_id, "version": version,
            "producer_id": producer_id}
    _REGISTRY[validator.domain] = validator


def get_validator(domain: str) -> Optional[TextArtifactValidator]:
    return _REGISTRY.get(domain)


def verify_validator_binding(domain: str,
                             validator: TextArtifactValidator
                             ) -> Optional[Dict[str, Any]]:
    """Use-site verification (growth_engine.py): the live validator must be
    byte-identical to the registered head for its domain.

    Returns the binding dict when binding is active; returns None when
    unbound (legacy path). Raises ``OracleBindingError`` on a silent swap or
    a tampered registration row -- the validator's verdict must not be
    trusted in that state.
    """
    if not _binding_active():
        return None
    from swarm_engine.governance.oracle_binding import OracleBindingError
    from swarm_engine.governance.oracle_binding import _digest as _d
    from swarm_engine.governance.binding_helpers import definition_digest_of
    binding = _REGISTRY_BINDINGS.get(domain)
    if binding is None:
        raise OracleBindingError(
            f"artifact validator for domain {domain!r} was never registered "
            f"under oracle binding -- refusing to validate")
    oracle_id, version = binding["oracle_id"], binding["version"]
    ok, why = _ORACLE_REGISTRY.verify_oracle_row(oracle_id, version)
    if not ok:
        raise OracleBindingError(
            f"artifact validator binding broken for domain {domain!r}: {why}")
    row = _ORACLE_REGISTRY.oracle_version_row(oracle_id, version)
    _, current_text = definition_digest_of(_ORACLE_REGISTRY,
                                           _validator_definition(validator))
    import hmac as _hmac
    if not _hmac.compare_digest(_d(current_text), row["definition_digest"]):
        raise OracleBindingError(
            f"artifact validator for domain {domain!r} does not match its "
            f"registered definition -- the registry entry was swapped after "
            f"registration; refusing to validate")
    return binding


def record_artifact_validation(binding: Dict[str, Any], domain: str,
                               artifact: str,
                               verdict: ValidationVerdict) -> str:
    """Chained record of which validator judged which artifact. No-op unless
    called with a binding (i.e. only in the bound configuration). Returns
    the evaluation id."""
    from swarm_engine.governance.binding_helpers import canonical_digest
    return _ENGINE_ORACLE.evaluate(
        binding["oracle_id"],
        {"domain": domain, "artifact_digest": canonical_digest(artifact)},
        verdict.as_dict(),
        input_ref="artifact_validation:" + domain,
        version=binding["version"],
        supplier_id=binding["producer_id"])


register_validator(GDScriptStructuralValidator())
register_validator(PythonAPIValidator())
_bpy_validator = PythonAPIValidator(required_import="bpy")
_bpy_validator.domain = "bpy"
register_validator(_bpy_validator)
