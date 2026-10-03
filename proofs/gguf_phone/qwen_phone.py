"""Phone-side Qwen inference via JNI bridge (GGUF work).

Uses the native llama.cpp library (libllama.so) via the LlamaBridge
Java class, called through Chaquopy's Java interop.

Two models:
- Bundled: SmolLM2-135M (139MB) — offline fallback, basic conversing
- Download: Qwen3-0.6B (610MB) — better quality, on-demand

Usage:
    from runtime.phone_llm.qwen_phone import PhoneLLM
    llm = PhoneLLM()  # uses bundled 135M by default
    text = llm.complete("Hello, how are you?")
"""
from __future__ import annotations

import os
from typing import Optional


# Model paths (relative to app's files dir; resolved at runtime via Java)
BUNDLED_MODEL_ASSET = "models/smollm2-135m-instruct-q8_0.gguf"
DOWNLOAD_MODEL_NAME = "qwen3-0.6b-q8_0.gguf"
DOWNLOAD_MODEL_URL = (
    "https://github.com/jaymarr1114-source/remor-releases/releases/"
    "download/models/qwen3-0.6b-q8_0.gguf"
)
DOWNLOAD_MODEL_SHA256 = ""  # Filled when the model is published


class PhoneLLM:
    """On-device LLM via JNI bridge to llama.cpp.

    Fail-closed: if the native lib or model is unavailable, complete()
    raises with the real reason (not a fabricated response).
    """

    def __init__(self, model_path: Optional[str] = None):
        self._bridge = None
        self._handle = 0
        self._model_path = model_path or self._bundled_model_path()
        self._init_bridge()

    def _bundled_model_path(self) -> str:
        """Resolve the bundled 135M model path via Android context."""
        try:
            from java import jclass
            # Get the app's files dir via the current activity
            # (Chaquopy provides the activity via com.chaquo.python.utils)
            from com.chaquo.python.utils import PythonUtils
            # Fallback: try the standard Android context path
            # The actual path is resolved at runtime on the device
            return BUNDLED_MODEL_ASSET
        except Exception:
            return BUNDLED_MODEL_ASSET

    def _init_bridge(self) -> None:
        """Load the LlamaBridge Java class via Chaquopy interop."""
        try:
            from java import jclass
            BridgeClass = jclass("com.remor.app.LlamaBridge")
            self._bridge = BridgeClass()
        except Exception as exc:
            raise RuntimeError(
                f"PhoneLLM: failed to load LlamaBridge (native lib missing?): "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def load_model(self, model_path: Optional[str] = None) -> None:
        """Load a GGUF model. Raises on failure (fail-closed)."""
        path = model_path or self._model_path
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"PhoneLLM: model not found: {path}. "
                f"Bundled model missing or download not completed."
            )
        try:
            self._handle = self._bridge.initModel(path)
        except Exception as exc:
            raise RuntimeError(
                f"PhoneLLM: initModel failed for {path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not self._handle:
            raise RuntimeError(
                f"PhoneLLM: initModel returned null handle for {path}"
            )

    def complete(self, prompt: str, max_tokens: int = 50) -> str:
        """Run one completion. Blocking; call off the UI thread."""
        if not self._handle:
            self.load_model()
        try:
            result = self._bridge.complete(self._handle, prompt, max_tokens)
        except Exception as exc:
            raise RuntimeError(
                f"PhoneLLM: complete failed: {type(exc).__name__}: {exc}"
            ) from exc
        if result is None:
            raise RuntimeError("PhoneLLM: complete returned null (native error)")
        return str(result)

    def close(self) -> None:
        """Free the model and release native memory."""
        if self._bridge and self._handle:
            try:
                self._bridge.freeModel(self._handle)
            except Exception:
                pass
            self._handle = 0

    def __enter__(self):
        self.load_model()
        return self

    def __exit__(self, *args):
        self.close()
