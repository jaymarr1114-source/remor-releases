"""PhoneLLM provider adapter — registers on-device GGUF inference as a provider.

This bridges the native phone LLM (SmolLM2-135M bundled, Qwen3-0.6B on-demand)
into the ProviderRegistry so the synthesis path can use it for ANSWER_FACTUAL
and other language tasks.

The adapter is fail-closed: if the native lib or model is unavailable,
call() raises CapabilityUnavailable with the real reason.
"""
from __future__ import annotations

from typing import Any, Dict, List

import sys
import os

# Add the proofs dir to path for the PhoneLLM import
_proofs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "proofs", "gguf_phone")
if os.path.isdir(_proofs_dir):
    sys.path.insert(0, _proofs_dir)

try:
    from qwen_phone import PhoneLLM
    _PHONE_LLM_AVAILABLE = True
except ImportError:
    _PHONE_LLM_AVAILABLE = False
    PhoneLLM = None  # type: ignore


from runtime.services.providers import ProviderAdapter

class PhoneLLMAdapter(ProviderAdapter):
    """ProviderAdapter for on-device GGUF inference.

    Operations:
    - "complete": run one text completion. Payload: {"prompt": str,
      "max_tokens": int}. Returns: {"text": str}.
    """

    name = "phone-llm"

    def __init__(self, model_path: str = None):
        self._llm = None
        self._model_path = model_path
        if not _PHONE_LLM_AVAILABLE:
            raise RuntimeError(
                "PhoneLLMAdapter: qwen_phone module not available. "
                "The native GGUF stack (libllama.so + LlamaBridge) is required."
            )

    def _ensure_llm(self):
        if self._llm is None:
            try:
                self._llm = PhoneLLM(model_path=self._model_path)
                self._llm.load_model()
            except Exception as exc:
                from swarm_engine.services.unavailable import CapabilityUnavailable
                raise CapabilityUnavailable(
                    capability="phone-llm",
                    reason=f"model load failed: {type(exc).__name__}: {exc}"
                ) from exc
        return self._llm

    def call(self, operation: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if operation != "complete":
            from swarm_engine.services.unavailable import CapabilityUnavailable
            raise CapabilityUnavailable(
                capability="phone-llm",
                reason=f"unknown operation '{operation}'. Supported: ['complete']"
            )
        prompt = payload.get("prompt", "")
        max_tokens = int(payload.get("max_tokens", 50))
        if not prompt:
            from swarm_engine.services.unavailable import CapabilityUnavailable
            raise CapabilityUnavailable(
                capability="phone-llm",
                reason="'prompt' is required"
            )
        try:
            llm = self._ensure_llm()
            text = llm.complete(prompt, max_tokens=max_tokens)
        except Exception as exc:
            from swarm_engine.services.unavailable import CapabilityUnavailable
            raise CapabilityUnavailable(
                capability="phone-llm",
                reason=f"completion failed: {type(exc).__name__}: {exc}"
            ) from exc
        return {"text": text}

    def operations(self) -> List[str]:
        return ["complete"]


def register_phone_llm(registry, model_path: str = None):
    """Register the PhoneLLM adapter with a ProviderRegistry.

    Returns the ProviderRecord, or None if the native stack is unavailable
    (in which case the registry stays empty and callers get honest
    CapabilityUnavailable — never a fabricated response).
    """
    if not _PHONE_LLM_AVAILABLE:
        return None
    adapter = PhoneLLMAdapter(model_path=model_path)
    return registry.register(adapter)
