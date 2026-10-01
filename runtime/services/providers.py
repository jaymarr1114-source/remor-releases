"""External-service / plugin integration front (§7).

TEST-ONLY (DEAD-CODE-1, 2026-10-01): imported only by
tests/track2_new/test_providers_fresh.py and
tests/track2b/test_providers_fresh.py; no production inlet wires this
module. The registry below is real but unwired.

Classification: ABSENT as a product capability. This module provides the
real, minimal seam -- a provider registry with explicit registration,
credential hygiene (the registry never logs, persists, or inspects
adapter credentials; adapters own their secrets), and honest refusal
when no provider is registered -- but ships ZERO built-in providers:

  * no provider-client abstraction existed in the tree (audited);
  * no credential store exists;
  * no provider SDKs or API credentials exist in this environment;
  * there is no real external service to integrate with here.

What IS real and tested: registration, lookup, unregistration, and the
call path to a registered adapter (exercised in tests with a test-only
adapter -- the product itself registers none). ``call()`` with no
matching provider raises CapabilityUnavailable (classification ABSENT),
never a fabricated result.

A future real provider (e.g. a TTS or image API) would implement
ProviderAdapter, own its credentials, and register here; services/voice.py
and services/media.py already accept a ``provider`` hint for that wiring.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.services.unavailable import CapabilityUnavailable


class ProviderAdapter:
    """Interface for an external-service adapter. Implementations own
    their credentials and transport; the registry never sees secrets."""

    name: str = "unnamed"

    def call(self, operation: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def operations(self) -> List[str]:
        return []


@dataclass
class ProviderRecord:
    name: str
    operations: List[str] = field(default_factory=list)


class ProviderRegistry:
    """Explicit registry of external-service adapters. Zero built-in
    providers: integration with any real external service is ABSENT until
    an adapter is registered."""

    def __init__(self):
        self._adapters: Dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> ProviderRecord:
        name = getattr(adapter, "name", "") or "unnamed"
        if not isinstance(name, str) or not name.strip():
            raise ValueError("adapter must have a non-empty string name")
        name = name.strip()
        if not isinstance(adapter, ProviderAdapter):
            raise ValueError("adapter must subclass ProviderAdapter "
                             "(no raw callables: identity is validated)")
        self._adapters[name] = adapter
        return ProviderRecord(name=name, operations=list(adapter.operations()))

    def unregister(self, name: str) -> bool:
        return self._adapters.pop(name, None) is not None

    def get(self, name: str) -> Optional[ProviderAdapter]:
        return self._adapters.get(name)

    def list(self) -> List[ProviderRecord]:
        return [ProviderRecord(name=n, operations=list(a.operations()))
                for n, a in self._adapters.items()]

    def call(self, provider_name: str, operation: str,
             payload: Optional[Dict[str, Any]] = None,
             producer: Optional[str] = None) -> Dict[str, Any]:
        adapter = self._adapters.get(provider_name)
        if adapter is None:
            raise CapabilityUnavailable(
                capability=f"provider.{provider_name}.{operation}",
                classification="ABSENT",
                reason=("no provider adapter registered under "
                        f"{provider_name!r}; external-service integration "
                        "is absent"),
                missing=["provider adapter registration",
                         "provider credentials / credential store"])
        if operation not in adapter.operations():
            raise CapabilityUnavailable(
                capability=f"provider.{provider_name}.{operation}",
                classification="ABSENT",
                reason=(f"provider {provider_name!r} does not offer "
                        f"operation {operation!r}"),
                missing=[])
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        # Credential hygiene: the registry passes the payload through and
        # records nothing about it.
        return adapter.call(operation, dict(payload or {}))
