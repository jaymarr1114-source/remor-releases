"""Substrate kinds: the execution substrate behind an agent.

A substrate is what an agent "thinks with". The organization is substrate-
independent: agents are identified by template + history, and the substrate
can be replaced without touching identity, assignments, or provenance.

- CallableSubstrate: an ordinary Python callable doing the work.
- SymbolicSubstrate: ordered predicate->emitter rules; first match wins.
- LLMSubstrate: real when wired (LLMSubstrateWiring: a GrantedCognitionProvider
  + grant issuer + charge mc). run() borrows cognition through the provider's
  frozen interface; the result carries borrowed:<model>@<revision> provenance.
  Unwired (provider None), run() raises SubstrateUnavailable -- honestly
  ABSENT, refusing rather than simulating. There is no fake LLM here, and
  no silent fallback to one.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

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
    """Governed LLM substrate: reasoning through a GrantedCognitionProvider.

    The provider is CONSUMED READ-ONLY through its frozen
    request_cognition interface -- never modified, never subclassed here.
    Every run():
      1. builds the prompt from the task,
      2. asks the provider's teacher for a cost estimate,
      3. requests a real grant from grant_issuer (U-6: the caller
         requests and receives the grant; the issuer is the explicit
         seam, injected at wiring time),
      4. calls provider.request_cognition through the REAL path --
         no grant -> the provider's genuine R_NO_GRANT refusal;
         insufficient grant -> genuine deferral with zero charge,
      5. maps the borrowed text into the substrate result contract with
         provenance "borrowed:<model>@<revision>" attached top-level.

    Constructed WITHOUT a provider (provider=None) the substrate is
    honestly ABSENT: run() raises SubstrateUnavailable -- the pre-
    wiring behavior, preserved so unwired services keep refusing
    rather than simulating.

    claimed_capabilities is [] by design: the substrate claims no
    authority on the model's behalf. The runner's capability gate
    passes an empty claim fail-closed; any authority the agent acts
    under comes from the assignment's scope, not from model text.
    """
    kind = "llm"

    #: Named native refusal is owned by the wiring's native tier (a
    #: refusing provider); kept here for documentation of the borrow
    #: justification chain.
    NATIVE_REFUSAL_NAME = "agent_org:no_native_reasoning_tier"

    def __init__(self, name: str = "llm_coder",
                 *, provider: Any = None,
                 grant_issuer: Optional[Callable[[float], Any]] = None,
                 mc_id: str = "",
                 version: str = "1"):
        self.name = name
        self.version = version
        self._provider = provider
        self._grant_issuer = grant_issuer
        self._mc_id = mc_id

    @property
    def wired(self) -> bool:
        """Whether a real provider is attached (else honest ABSENT)."""
        return self._provider is not None

    def describe(self) -> Dict[str, Any]:
        base = {"kind": self.kind, "name": self.name,
                "version": self.version,
                "substrate_id": self.substrate_id}
        if self._provider is None:
            base["status"] = ("ABSENT: no provider wired -- template "
                               "exists, instantiation refused")
            return base
        teacher = getattr(self._provider, "teacher", None)
        base["status"] = ("REAL: governed borrow via "
                          "GrantedCognitionProvider (frozen interface)")
        base["teacher"] = {
            "model_id": getattr(teacher, "model_id", "?"),
            "revision": getattr(teacher, "revision", "?"),
        }
        base["mc_id"] = self._mc_id
        return base

    def _build_prompt(self, task: Dict[str, Any]) -> str:
        import json as _json
        body = _json.dumps(task, sort_keys=True, default=str)
        return ("You are a coding agent. Reason about the task below and "
                "produce the implementation as your answer.\n"
                f"Task: {body}")

    def run(self, task: Dict[str, Any]) -> Dict[str, Any]:
        if self._provider is None:
            raise SubstrateUnavailable(
                f"{self.substrate_id}: no cognition provider wired "
                f"(honest ABSENT). Refusing rather than simulating.")
        task = dict(task or {})
        prompt = self._build_prompt(task)
        estimate = float(
            self._provider.teacher.estimate_cost_s(prompt))
        grant = (self._grant_issuer(estimate)
                 if self._grant_issuer is not None else None)
        ctx: Dict[str, Any] = {
            "purpose": "agent_org:llm_substrate_run",
            "target_profile": "agent_org:llm",
        }
        # Grantless -> the provider's genuine no-grant refusal fires on
        # the real path (never a substrate-side pre-check).
        if grant is not None:
            ctx["frm_grant"] = grant
        started = time.monotonic()
        cres = self._provider.request_cognition(
            mc_id=self._mc_id, prompt=prompt, context=ctx)
        latency_s = time.monotonic() - started
        if not cres.ok:
            outcome = (cres.error or "").split(":")[0]
            raise LLMCognitionRefused(
                f"{self.substrate_id}: provider refused/deferred: "
                f"{cres.error}", outcome=outcome)
        grant_id = getattr(grant, "grant_id", "") or ""
        consumed = (self._provider.grant_consumed_s(grant_id)
                    if grant_id else 0.0)
        params = task.get("params")
        result = {
            "implementation": cres.text,
            "entrypoint": str(task.get("entrypoint", "")),
            "technique": "borrowed_cognition",
            "params": params if isinstance(params, dict) else {},
            "claimed_capabilities": [],
            "measurements": {
                "cognition_latency_s": round(latency_s, 3),
                "grant_consumed_s": round(float(consumed), 3),
                "grant_budget_s": float(getattr(
                    grant, "budget_s", 0.0) or 0.0),
                "native_refusal": cres.native_refusal,
                "result_id": cres.result_id,
                "mc_id": self._mc_id,
            },
            "notes": (f"LLM substrate via GrantedCognitionProvider; "
                      f"{cres.provenance}; {latency_s:.1f}s wall; "
                      f"task_chars={len(prompt)}"),
            "provenance": cres.provenance,
        }
        return _check_result(result, self.substrate_id)


class LLMCognitionRefused(ExecutionError):
    """The governed provider refused or deferred a borrow for this run.

    Carries the provider's verbatim error and the outcome prefix
    (cognition_refused / cognition_deferred / cognition_failed).
    Raised -- not returned -- so the AgentRunner's failure path records
    it: an agent that could not think under its grant is a real
    execution failure, never a silent downgrade.
    """

    def __init__(self, message: str, *, outcome: str = "") -> None:
        super().__init__(message)
        self.outcome = outcome


class LLMSubstrateWiring:
    """The explicit llm seam: provider + grant issuer + charge identity.

    Built once by the services seam (build_llm_wiring) and handed to
    AgentFactory. The factory builds one LLMSubstrate per llm-template
    instantiation from this wiring; every substrate from one wiring
    shares the provider and the charge mc.
    """

    def __init__(self, *, provider: Any,
                 grant_issuer: Callable[[float], Any],
                 mc_id: str) -> None:
        if provider is None:
            raise ValueError("LLMSubstrateWiring requires a provider")
        if grant_issuer is None:
            raise ValueError("LLMSubstrateWiring requires a grant_issuer")
        if not mc_id:
            raise ValueError("LLMSubstrateWiring requires an mc_id")
        self.provider = provider
        self.grant_issuer = grant_issuer
        self.mc_id = mc_id

    def build_substrate(self, name: str = "llm_coder",
                        version: str = "1") -> LLMSubstrate:
        return LLMSubstrate(name, provider=self.provider,
                            grant_issuer=self.grant_issuer,
                            mc_id=self.mc_id, version=version)
