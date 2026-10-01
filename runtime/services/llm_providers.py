"""LLM provider registry: the open slots for reasoning providers.

James's U-10 decision (2026-10-01): the http_adapter/LLM-wiring
non-connection was deliberate -- the serving path was designed so users
could plug in their own AI accounts. This registry is the mechanism:
named providers, one default, all registered through the same public
path. The built-in Qwen3 wiring registers here like any other provider
(dogfooding); a user's own AI account or bot registers the same way.

Provider contract (what a provider must be):
  * Implements the CognitionProvider Protocol
    (runtime/core/microcontroller/substrate.py):
    request_cognition(*, mc_id, prompt, context) -> CognitionResult
  * Honors the governance contract: reads context["frm_grant"];
    no grant -> honest refusal (never a silent ungoverned call);
    records provenance on every result.
  * Fails closed: on internal error returns CognitionResult(ok=False)
    with a named error, never raises into the serving path, never
    returns plausible-looking text on failure.

A registry entry bundles the three things a serving-path call needs:
  * provider: the CognitionProvider
  * grant_issuer: Callable[[float], FrmGrant] -- issues a real grant
    for the estimated cost (the U-6 seam: the caller requests and
    receives the grant; issuance policy lives with the provider)
  * mc_id: the charge identity completions are billed against

Thread safety: the registry is safe for concurrent serving-path use.
Provider *calls* serialize per-provider only if the provider says so;
the registry itself never blocks a call.

What this is NOT:
  * Not a second grant authority. Grant issuance policy stays with
    each provider's wiring (today: build_llm_wiring's per-call issuer;
    future: the FRM epoch loop).
  * Not a model manager. Weights, revisions, and teacher selection
    stay inside each provider's own construction.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass(frozen=True)
class ProviderEntry:
    """One registered reasoning provider: the three serving-path needs."""
    name: str
    provider: Any  # CognitionProvider Protocol
    grant_issuer: Callable[[float], Any]  # (estimated_cost_s) -> FrmGrant
    mc_id: str
    description: str = ""


class ProviderRegistry:
    """Named, thread-safe registry of reasoning providers.

    The serving path asks for the default; operators (or users plugging
    in their own AI accounts) register additional providers without
    touching the serving path.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._providers: Dict[str, ProviderEntry] = {}
        self._default: Optional[str] = None

    def register(self, entry: ProviderEntry,
                 *, make_default: bool = False) -> None:
        """Register a provider. The FIRST registration becomes the default
        unless make_default selects another. Re-registering a name
        replaces the entry (documented, not silent: the old entry is
        returned by unregister first if the caller cares)."""
        if not entry.name:
            raise ValueError("provider name must be non-empty")
        if entry.provider is None:
            raise ValueError("provider must not be None")
        if entry.grant_issuer is None:
            raise ValueError("grant_issuer must not be None")
        if not entry.mc_id:
            raise ValueError("mc_id must be non-empty")
        with self._lock:
            self._providers[entry.name] = entry
            if make_default or self._default is None:
                self._default = entry.name

    def unregister(self, name: str) -> Optional[ProviderEntry]:
        """Remove a provider. If it was the default, the default falls
        back to an arbitrary remaining provider (insertion order), or
        None when the registry is empty."""
        with self._lock:
            old = self._providers.pop(name, None)
            if self._default == name:
                self._default = next(iter(self._providers), None)
            return old

    def get(self, name: str) -> Optional[ProviderEntry]:
        with self._lock:
            return self._providers.get(name)

    def default(self) -> Optional[ProviderEntry]:
        """The default provider, or None when the registry is empty
        (the serving path must then refuse honestly, never hallucinate)."""
        with self._lock:
            if self._default is None:
                return None
            return self._providers.get(self._default)

    def set_default(self, name: str) -> None:
        with self._lock:
            if name not in self._providers:
                raise KeyError(f"no provider registered as {name!r}")
            self._default = name

    def list(self) -> List[str]:
        with self._lock:
            return list(self._providers.keys())

    def default_name(self) -> Optional[str]:
        with self._lock:
            return self._default


# ---------------------------------------------------------------------------
# the built-in provider, registered through the public path (dogfooding)
# ---------------------------------------------------------------------------

BUILTIN_PROVIDER_NAME = "remor-builtin-qwen3"


def register_builtin_provider(registry: ProviderRegistry, *,
                              gguf_path: Optional[str] = None,
                              llama_cli: Optional[str] = None,
                              make_default: bool = True) -> ProviderEntry:
    """Build the Qwen3 wiring and register it through the public
    register() path -- the exact call a user provider's installer would
    make. Raises FileNotFoundError when weights or llama-cli are absent
    (honest about missing substrate; the registry stays empty and the
    serving path refuses honestly)."""
    from runtime.services.agent_api import build_llm_wiring
    wiring = build_llm_wiring(gguf_path=gguf_path, llama_cli=llama_cli)
    entry = ProviderEntry(
        name=BUILTIN_PROVIDER_NAME,
        provider=wiring.provider,
        grant_issuer=wiring.grant_issuer,
        mc_id=wiring.mc_id,
        description=(
            "Built-in Qwen3-8B provider behind the governed cognition "
            "inlet (FRM grants, provenance, telemetry). Registered "
            "through the public provider path like any user provider."),
    )
    registry.register(entry, make_default=make_default)
    return entry
