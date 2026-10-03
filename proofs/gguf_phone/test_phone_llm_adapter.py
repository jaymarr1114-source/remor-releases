"""Test: PhoneLLM provider adapter (GGUF work).

Verifies:
1. Adapter registers with ProviderRegistry via the public API.
2. Adapter exposes the "complete" operation.
3. On bench (no native lib), call() raises CapabilityUnavailable honestly —
   never a fabricated response.
"""
import sys
import os

sys.path.insert(0, "/home/hatch/workspace/remor_convergence/canonical/runtime")
sys.path.insert(0, "/home/hatch/workspace/remor_convergence/canonical")

from runtime.services.providers import ProviderRegistry
from runtime.services.phone_llm_adapter import (
    PhoneLLMAdapter, register_phone_llm, _PHONE_LLM_AVAILABLE,
)
from swarm_engine.services.unavailable import CapabilityUnavailable


def test_adapter_registers():
    """Adapter registers via public API."""
    if not _PHONE_LLM_AVAILABLE:
        print("SKIP: qwen_phone not importable (expected on bench without Chaquopy)")
        return
    registry = ProviderRegistry()
    record = register_phone_llm(registry)
    assert record is not None, "registration returned None"
    assert record.name == "phone-llm", f"wrong name: {record.name}"
    assert "complete" in record.operations, f"missing 'complete': {record.operations}"
    retrieved = registry.get("phone-llm")
    assert retrieved is not None, "registry.get returned None"
    print("PASS: adapter registers with ProviderRegistry")


def test_adapter_fails_honestly_without_native():
    """On bench (no native lib), call() raises CapabilityUnavailable."""
    registry = ProviderRegistry()
    # Directly instantiate the adapter (bypasses _PHONE_LLM_AVAILABLE check
    # by mocking the import — we test the fail-closed path)
    adapter = PhoneLLMAdapter.__new__(PhoneLLMAdapter)
    adapter._llm = None
    adapter._model_path = "/nonexistent/model.gguf"
    registry.register(adapter)

    retrieved = registry.get("phone-llm")
    assert retrieved is not None

    try:
        retrieved.call("complete", {"prompt": "Hello", "max_tokens": 10})
        print("FAIL: call() should have raised CapabilityUnavailable")
        sys.exit(1)
    except CapabilityUnavailable as e:
        # Honest failure — the real reason, not a fabricated response
        assert "phone-llm" in str(e).lower() or "failed" in str(e).lower(), \
            f"error doesn't name the cause: {e}"
        print(f"PASS: call() raises CapabilityUnavailable honestly: {e}")
    except Exception as e:
        print(f"FAIL: wrong exception type: {type(e).__name__}: {e}")
        sys.exit(1)


def test_unknown_operation_fails():
    """Unknown operations raise CapabilityUnavailable, not silent None."""
    adapter = PhoneLLMAdapter.__new__(PhoneLLMAdapter)
    adapter._llm = None
    adapter._model_path = None
    try:
        adapter.call("nonexistent_op", {})
        print("FAIL: should have raised CapabilityUnavailable")
        sys.exit(1)
    except CapabilityUnavailable:
        print("PASS: unknown operation raises CapabilityUnavailable")


if __name__ == "__main__":
    test_adapter_registers()
    test_adapter_fails_honestly_without_native()
    test_unknown_operation_fails()
    print("\nAll PhoneLLM adapter tests passed.")
