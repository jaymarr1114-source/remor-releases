"""runtime/services/chat_api.py

The default chat-serving inlet: production chat turns enter here.

One REMOR. The caller passes a prompt and (optionally) think_hard=True;
the inlet issues FRM grants from the single shared issuance path,
runs the turn through the AutoRouter's hidden execution hierarchy, and
returns the served response with its labels (final / provisional /
deeper). The caller never chooses a brain -- "think hard" is a mode
parameter, never a substrate selector.

Grant issuance: every grant comes from
swarm_engine.curiosity.frm.grant.issue_run_grant -- the same function
the agent-org llm wiring uses. There is exactly one per-run issuer.
Bound (disclosed, inherited): per-run issuance, not the FRM epoch loop.

Charging: the student charges a real MicrocontrollerSubstrate
(cooperative budget); the deep path charges through the llm wiring's
own substrate. Both are real depleting budgets, not doubles.

Enforcement state: read from the real enforcement-state store via
read_api when a state dir is configured (no record -> RUNNING, the
authority's own bootstrap semantic); a local override exists for
tests and for operators without a store. The override is a real
branch consulted on every turn, not a test-only hook.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_TREE = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (os.path.join(_TREE, "pylib"),
           os.path.join(_TREE, "distill1"),
           _TREE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from swarm_engine.curiosity.frm.grant import (  # noqa: E402
    FrmGrant, issue_run_grant)
from swarm_engine.core.microcontroller.substrate import (  # noqa: E402
    MicrocontrollerSubstrate)
from governed_student import GovernedStudent  # noqa: E402
from router import AutoRouter  # noqa: E402

STUDENT_GRANT_MARGIN_S = 3.0
DEEP_GRANT_MARGIN_S = 120.0
GRANT_DOMAIN = "chat"


from swarm_engine.services.enforcement_core import (
    make_enforcement_reader as _make_enforcement_reader)


def _read_enforcement_state(state_dir: Optional[str]) -> str:
    """Real enforcement state — delegates to the shared core.

    STUDENT-ENFORCE-1: one enforcement core for both paths. This is a
    thin wrapper preserving the local name; the logic lives in
    enforcement_core.read_enforcement_state.
    """
    from swarm_engine.services.enforcement_core import (
        read_enforcement_state)
    return read_enforcement_state(state_dir)


def _entry_to_wiring(entry: Any) -> Any:
    """Adapt a ProviderEntry to the wiring shape the router expects.

    SERVE-WIRE-1: the router needs (provider, grant_issuer, mc_id).
    The registry entry already bundles these three — this is a thin
    namespace adapter, not a second wiring path.
    """
    class _Wiring:
        pass
    w = _Wiring()
    w.provider = entry.provider
    w.grant_issuer = entry.grant_issuer
    w.mc_id = entry.mc_id
    # The deep-grant path uses provider.teacher; the entry's provider
    # is the full CognitionProvider with .teacher.
    return w


class ChatService:
    """The production chat inlet over the hidden execution hierarchy."""

    def __init__(self, base_dir: str, *,
                 llm_wiring: Any = None,
                 provider_registry: Any = None,
                 enforcement_state_dir: Optional[str] = None,
                 enforcement_state: Optional[Callable[[], str]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()

        # -- deep path: via the provider registry (SERVE-WIRE-1) --------
        # The builtin provider registers through the public
        # register_builtin_provider() path — the exact call a user
        # provider's installer would make. No hardwired build_llm_wiring().
        if llm_wiring is None:
            if provider_registry is None:
                from runtime.services.llm_providers import (
                    ProviderRegistry, register_builtin_provider)
                provider_registry = ProviderRegistry()
                try:
                    register_builtin_provider(provider_registry)
                except FileNotFoundError:
                    # Honest: no substrate, registry stays empty.
                    # The serving path will refuse with the real reason.
                    pass
            entry = provider_registry.default()
            if entry is None:
                raise RuntimeError(
                    "no LLM provider registered: the serving path "
                    "requires a provider via the registry")
            # Adapt the registry entry to the wiring shape the router
            # expects (provider + grant_issuer + mc_id).
            llm_wiring = _entry_to_wiring(entry)
        self._llm = llm_wiring
        self._provider_registry = provider_registry

        # -- student path: its own budget pool on a real substrate ------
        self._student_substrate = MicrocontrollerSubstrate()
        self._student_substrate.register_loop("run", budget_s=7200.0)
        spawned = self._student_substrate.spawn(
            "run", purpose="chat-inlet-student", budget_s=3600.0)
        if not spawned.ok:
            raise RuntimeError(
                "could not spawn charge mc for chat student: "
                f"{spawned.refusal}")
        self._student_mc_id = spawned.mc.mc_id
        self._student = GovernedStudent(
            self._student_substrate, self._student_mc_id, clock=clock)

        # -- enforcement state: real store, overridable ------------------
        # STUDENT-ENFORCE-1: shared enforcement core.
        self._enforcement_state = _make_enforcement_reader(
            enforcement_state_dir, override=enforcement_state)

        self._router = AutoRouter(
            self._student, self._llm.provider,
            mc_id="chat-inlet",
            enforcement_state=self._enforcement_state,
            clock=clock)

    # -- grant issuance: the single shared path --------------------------
    def issue_student_grant(self, prompt: str) -> FrmGrant:
        return issue_run_grant(
            domain=GRANT_DOMAIN,
            estimated_cost_s=GovernedStudent.estimate_cost_s(prompt),
            margin_s=STUDENT_GRANT_MARGIN_S,
            note="chat inlet: per-turn student grant")

    def issue_deep_grant(self, prompt: str) -> FrmGrant:
        return issue_run_grant(
            domain=GRANT_DOMAIN,
            estimated_cost_s=self._llm.provider.teacher.estimate_cost_s(
                prompt),
            margin_s=DEEP_GRANT_MARGIN_S,
            note="chat inlet: per-turn deep grant")

    # -- the inlet --------------------------------------------------------
    def chat(self, prompt: str, *, think_hard: bool = False) -> Dict[str, Any]:
        """One production chat turn. Returns the router's result with the
        served section carrying the final response contract:

          served: {text, label, via, materially_better, diff_ratio, note?}

        Labels are part of the served output, never just internals.
        Never raises on governance grounds: refusals, deferrals, and
        transport failures arrive as real reasons in the result, with
        zero phantom charges.
        """
        with self._lock:
            # Issuance failures fail closed: a refusal with the real
            # reason, never an exception, zero charges by construction
            # (no grant was ever issued).
            try:
                student_grant = self.issue_student_grant(prompt)
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "fast": {"ok": False,
                             "error": f"inlet_refused:issuance_failed: "
                                      f"{type(exc).__name__}: {exc}",
                             "charged_s": 0.0},
                    "escalation": {"triggered": False, "mode": "none"},
                    "deep": {"attempted": False},
                    "served": {
                        "text": "[final] I couldn't answer that: "
                                "the resource grant could not be issued "
                                f"({type(exc).__name__}).",
                        "label": "[final]",
                        "via": "none",
                        "materially_better": False,
                        "diff_ratio": None,
                    },
                    "inlet": {"student_grant_id": None,
                              "deep_grant_id": None},
                }
            escalate, _reasons = AutoRouter.should_escalate(
                prompt, think_hard=think_hard)
            try:
                deep_grant = (self.issue_deep_grant(prompt)
                              if escalate else None)
            except Exception as exc:  # noqa: BLE001
                # Student grant stands; deep path honestly unavailable.
                deep_grant = None
                deep_issuance_error = (
                    f"inlet: deep grant issuance failed: "
                    f"{type(exc).__name__}: {exc}")
            else:
                deep_issuance_error = None
            result = self._router.chat(
                prompt,
                student_grant=student_grant,
                deep_grant=deep_grant,
                think_hard=think_hard,
                deep_mc_id=self._llm.mc_id)
            if deep_issuance_error is not None:
                # The router saw no deep grant; record why honestly.
                result["deep"].setdefault("note", deep_issuance_error)
                served = result.get("served") or {}
                if served.get("note"):
                    served["note"] += "; " + deep_issuance_error
                else:
                    served["note"] = deep_issuance_error
            # The student turn goes to the standard persistent student
            # server (127.0.0.1:18080, via distill1/server_lifecycle.sh);
            # a down server fails the turn honestly with the transport
            # reason (proven in FRM-STUDENT-1 p7), never silently.
            result["inlet"] = {
                "student_grant_id": student_grant.grant_id,
                "deep_grant_id": (deep_grant.grant_id
                                  if deep_grant is not None else None),
            }
            return result

    def grant_consumed_s(self, grant_id: str) -> float:
        """Seconds consumed against a student grant id (observability)."""
        return self._student.grant_consumed_s(grant_id)
