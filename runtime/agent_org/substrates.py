"""Substrate kinds: the execution substrate behind an agent.

A substrate is what an agent "thinks with". The organization is substrate-
independent: agents are identified by template + history, and the substrate
can be replaced without touching identity, assignments, or provenance.

- CallableSubstrate: an ordinary Python callable doing the work.
- SymbolicSubstrate: ordered predicate->emitter rules; first match wins.
- LLMSubstrate: HONESTLY ABSENT. The template may exist (tpl_llm_coder_v1)
  but instantiation is refused -- run() always raises SubstrateUnavailable.
  There is no fake LLM here, and no silent fallback to one.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List

from swarm_engine.agent_org.exceptions import ExecutionError


class SubstrateUnavailable(Exception):
    """Raised when a substrate kind cannot be instantiated or run."""


# Result-dict keys every substrate must produce. claimed_capabilities is
# deliberately NOT validated here: the AgentRunner owns the authority gate
# and must refuse (AuthorityError) a result missing that key. Fail closed
# at the gate, not at the substrate.
REQUIRED_RESULT_KEYS = ("implementation", "entrypoint", "technique",
                        "params", "measurements", "notes")


def _check_result(result: Any, substrate_id: str) -> Dict[str, Any]:
    if not isinstance(result, dict):
        raise ExecutionError(
            f"{substrate_id}: substrate result is not a dict "
            f"(got {type(result).__name__})")
    missing = [k for k in REQUIRED_RESULT_KEYS if k not in result]
    if missing:
        raise ExecutionError(
            f"{substrate_id}: substrate result missing keys {missing}")
    if not isinstance(result.get("implementation"), str):
        raise ExecutionError(
            f"{substrate_id}: 'implementation' must be code (str)")
    return result


class Substrate(ABC):
    kind: str = "?"
    name: str = "?"
    version: str = "1"

    @property
    def substrate_id(self) -> str:
        return f"{self.kind}:{self.name}:{self.version}"

    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        ...

    @abstractmethod
    def run(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Run the substrate on a JSON-serializable task dict.

        Returns a JSON-serializable result dict with keys:
        implementation (code str), entrypoint, technique, params,
        claimed_capabilities (list), measurements (dict), notes (str).
        """


class CallableSubstrate(Substrate):
    kind = "callable"

    def __init__(self, name: str, fn: Callable[[Dict[str, Any]], Dict[str, Any]],
                 version: str = "1"):
        self.name = name
        self.version = version
        self._fn = fn

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name,
                "version": self.version,
                "substrate_id": self.substrate_id,
                "callable": getattr(self._fn, "__name__", repr(self._fn))}

    def run(self, task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self._fn(task)
        except SubstrateUnavailable:
            raise
        except Exception as exc:
            raise ExecutionError(
                f"{self.substrate_id}: substrate raised "
                f"{type(exc).__name__}: {exc}") from exc
        return _check_result(result, self.substrate_id)


class SymbolicSubstrate(Substrate):
    """Ordered rules: [{"when": pred(task)->bool, "emit": fn(task)->dict}].

    First matching rule wins. The rule only orders/selects; the emitted
    result must still satisfy the substrate result contract.
    """
    kind = "symbolic"

    def __init__(self, name: str, rules: List[Dict[str, Any]],
                 version: str = "1"):
        self.name = name
        self.version = version
        self.rules = list(rules)
        for i, rule in enumerate(self.rules):
            if not callable(rule.get("when")) or not callable(rule.get("emit")):
                raise ValueError(f"rule {i}: 'when' and 'emit' must be callable")

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name,
                "version": self.version,
                "substrate_id": self.substrate_id,
                "n_rules": len(self.rules)}

    def run(self, task: Dict[str, Any]) -> Dict[str, Any]:
        for index, rule in enumerate(self.rules):
            try:
                matched = bool(rule["when"](task))
            except Exception as exc:
                raise ExecutionError(
                    f"{self.substrate_id}: rule {index} predicate raised "
                    f"{type(exc).__name__}: {exc}") from exc
            if matched:
                try:
                    result = rule["emit"](task)
                except SubstrateUnavailable:
                    raise
                except Exception as exc:
                    raise ExecutionError(
                        f"{self.substrate_id}: rule {index} emitter raised "
                        f"{type(exc).__name__}: {exc}") from exc
                checked = _check_result(result, self.substrate_id)
                meas = dict(checked.get("measurements") or {})
                meas["symbolic_rule_fired"] = index
                checked["measurements"] = meas
                return checked
        raise ExecutionError(
            f"{self.substrate_id}: no rule matched task {sorted(task)}")


class LLMSubstrate(Substrate):
    """Honest ABSENT substrate. The template exists; instantiation of the
    capability does not. run() always raises SubstrateUnavailable -- there
    is no simulated LLM, no fallback, no silent substitution."""
    kind = "llm"

    def __init__(self, name: str = "llm_coder", version: str = "1"):
        self.name = name
        self.version = version

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name,
                "version": self.version,
                "substrate_id": self.substrate_id,
                "status": "ABSENT: template exists, instantiation refused"}

    def run(self, task: Dict[str, Any]) -> Dict[str, Any]:
        raise SubstrateUnavailable(
            f"{self.substrate_id}: no LLM substrate is available in this "
            f"build (honest ABSENT). Refusing rather than simulating.")
