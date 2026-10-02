"""Reference user provider (USER-PROVIDER-1).

A user-supplied API-key-based reasoning provider implementing the
provider contract from llm_providers.py. This is the reference
implementation showing how a user's own AI account plugs in through
the exact same registration path as the builtin.

Contract:
  * provider: CognitionProvider — request_cognition(*, mc_id, prompt, context)
  * grant_issuer: Callable[[float], FrmGrant]
  * mc_id: charge identity

Credential hygiene: the API key comes from the environment
(REMOR_USER_PROVIDER_API_KEY), never hardcoded, never logged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

PROVIDER_NAME = "user-reference-apikey"
ENV_API_KEY = "REMOR_USER_PROVIDER_API_KEY"


@dataclass
class CognitionResult:
    """Minimal CognitionResult shape the contract requires."""
    ok: bool
    text: str = ""
    provenance: str = ""
    native_refusal: Optional[str] = None
    error: Optional[str] = None


class ApiKeyProvider:
    """API-key-based reasoning provider.

    In production, request_cognition would POST to the user's configured
    endpoint with the API key in the Authorization header. Here the
    transport is injectable so the contract can be proven without a real
    external service.
    """

    def __init__(self, *, api_key: str,
                 transport: Optional[Callable[[str, str], str]] = None,
                 endpoint: str = "https://api.example.com/v1/chat"):
        if not api_key:
            raise ValueError("api_key must be non-empty (from env, never hardcoded)")
        # The key is held, never logged or included in repr.
        self._api_key = api_key
        self._endpoint = endpoint
        self._transport = transport or self._default_transport

    def _default_transport(self, prompt: str, api_key: str) -> str:
        raise RuntimeError(
            "no transport configured: set a transport or endpoint for "
            "the real API call")

    def request_cognition(self, *, mc_id: str, prompt: str,
                          context: dict) -> CognitionResult:
        grant = (context or {}).get("frm_grant")
        if grant is None:
            return CognitionResult(
                ok=False,
                error="refused: no frm_grant in context (governance contract)",
                provenance=f"{PROVIDER_NAME}:refusal")
        try:
            text = self._transport(prompt, self._api_key)
        except Exception as exc:
            return CognitionResult(
                ok=False,
                error=f"transport_failed:{type(exc).__name__}: {exc}",
                provenance=f"{PROVIDER_NAME}:transport-error")
        return CognitionResult(
            ok=True, text=text,
            provenance=f"{PROVIDER_NAME}:api")


def build_user_provider(*,
                        transport: Optional[Callable[[str, str], str]] = None
                        ) -> tuple:
    """Build (provider, grant_issuer, mc_id) from the environment.

    Raises FileNotFoundError when the API key is absent — honest about
    missing credentials, like register_builtin_provider is about missing
    weights.
    """
    api_key = os.environ.get(ENV_API_KEY)
    if not api_key:
        raise FileNotFoundError(
            f"{ENV_API_KEY} not set: user provider credentials absent")
    provider = ApiKeyProvider(api_key=api_key, transport=transport)

    def grant_issuer(estimated_cost_s: float):
        # Per-call issuer (same pattern as builtin): a real grant object
        # shape the serving path expects.
        return type("FrmGrant", (), {
            "grant_id": f"user-{os.urandom(4).hex()}",
            "estimated_cost_s": estimated_cost_s,
        })()

    return provider, grant_issuer, f"user-provider-{PROVIDER_NAME}"


def register_user_provider(registry, *,
                           transport: Optional[Callable[[str, str], str]] = None,
                           make_default: bool = False):
    """Register through the public registry.register() path."""
    from swarm_engine.services.llm_providers import ProviderEntry
    provider, grant_issuer, mc_id = build_user_provider(transport=transport)
    entry = ProviderEntry(
        name=PROVIDER_NAME,
        provider=provider,
        grant_issuer=grant_issuer,
        mc_id=mc_id,
        description="Reference user API-key provider (USER-PROVIDER-1).",
    )
    registry.register(entry, make_default=make_default)
    return entry
