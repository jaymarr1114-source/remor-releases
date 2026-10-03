package com.remor.app;

import android.util.Log;

/**
 * JNI bridge to llama.cpp for on-device GGUF inference.
 *
 * Loads libllama.so (built for arm64-v8a via NDK) and exposes
 * model init / completion / free via JNI.
 *
 * Called from Python via Chaquopy's Java interop:
 *   from java import jclass
 *   Bridge = jclass("com.remor.app.LlamaBridge")
 *   bridge = Bridge()
 *   handle = bridge.initModel("/path/to/model.gguf")
 *   text = bridge.complete(handle, "Hello", 50)
 *   bridge.freeModel(handle)
 */
public class LlamaBridge {
    private static final String TAG = "LlamaBridge";

    static {
        try {
            System.loadLibrary("llama");
            Log.i(TAG, "libllama.so loaded");
        } catch (UnsatisfiedLinkError e) {
            Log.e(TAG, "Failed to load libllama.so: " + e.getMessage());
        }
    }

    /**
     * Initialize a GGUF model. Returns a native handle (pointer as long),
     * or 0 on failure.
     */
    public native long initModel(String modelPath);

    /**
     * Run one completion. Returns the generated text, or null on error.
     * This is a blocking call — run it on a background thread.
     */
    public native String complete(long handle, String prompt, int maxTokens);

    /**
     * Free the model and release native memory.
     */
    public native void freeModel(long handle);
}
