# GGUF Phone-Side Inference Design

## Goal
Run Qwen3-0.6B (610MB Q8_0 GGUF) on Android via Chaquopy, giving the phone
local language capability without the 4.7GB 8B weights.

## Architecture: 3 Layers

```
Python (Chaquopy)          payload/runtime/phone_llm/qwen_phone.py
    ↓ (Chaquopy Java interop)
Java                        com.remor.app.LlamaBridge.java
    ↓ (JNI)
Native (C++)                llama.cpp built for arm64-v8a via NDK
    ↓
GGUF Model                  Qwen3-0.6B-Q8_0.gguf (610MB)
```

### Layer 1: Native (llama.cpp for Android)
- Build llama.cpp with Android NDK for arm64-v8a (and armeabi-v7a for 32-bit)
- Output: `libllama.so` + `libggml*.so` in `app/src/main/jniLibs/arm64-v8a/`
- Use NDK r26d, cmake toolchain file from NDK

### Layer 2: JNI Bridge (Java)
`com.remor.app.LlamaBridge`:
```java
public class LlamaBridge {
    static { System.loadLibrary("llama"); }
    public native long initModel(String modelPath);
    public native String complete(long handle, String prompt, int maxTokens);
    public native void freeModel(long handle);
}
```
- JNI C++ code wraps llama.cpp common API
- Model handle is a native pointer (long)

### Layer 3: Python API (Chaquopy)
`payload/runtime/phone_llm/qwen_phone.py`:
```python
class PhoneQwen:
    """Qwen3-0.6B on phone via JNI bridge."""
    def __init__(self, model_path: str):
        from java import dynamic_proxy  # Chaquopy
        # ... load LlamaBridge via Chaquopy Java interop
    def complete(self, prompt: str, max_tokens: int = 50) -> str: ...
```

## Model Delivery
**Decision: Download on demand** (not bundled in APK)
- 610MB is too large for APK bundling (would make app unusable)
- Download from GitHub releases (same as update bundles) with SHA256 verification
- Store in app's private files dir: `context.getFilesDir() / "models" / "qwen3-0.6b-q8_0.gguf"`
- Resume support, progress UI, WiFi-only by default

## Memory Budget
- Qwen3-0.6B Q8_0: ~610MB weights + ~200MB KV cache + overhead = ~900MB peak
- Modern phones have 6-12GB RAM; 900MB is acceptable for a foreground task
- Release model when not in use (freeModel)

## Integration Points
1. **Synthesis path**: `task_interface.py` ANSWER_FACTUAL now routes to synthesis;
   on phone, synthesis uses PhoneQwen when available, else escalates to bench
2. **Provider registry**: Register as `phone-qwen-0.6b` provider via public API
3. **FRM grants**: Same grant-gating as bench providers (no bypass)

## Build Steps
1. [x] Download Android NDK r26d
2. [ ] Clone llama.cpp, build for arm64-v8a with NDK
3. [ ] Write JNI bridge (Java + C++)
4. [ ] Write Python wrapper (Chaquopy interop)
5. [ ] Model download manager (with SHA256, resume)
6. [ ] Integration with synthesis path
7. [ ] On-device test

## License
Qwen3-0.6B GGUF: Apache 2.0 (same as Qwen3-8B). Verified per standing rule.
llama.cpp: MIT. Both license-clean for commercial use.
