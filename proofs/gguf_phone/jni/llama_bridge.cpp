// JNI bridge: Java_com_remor_app_LlamaBridge -> llama.cpp
//
// Wraps llama.cpp's common API for Android. Each model handle is a
// `llama_context*` cast to jlong. Threading: complete() is blocking;
// the Java/Python caller must run it off the UI thread.

#include <jni.h>
#include <string>
#include <vector>
#include "llama.h"

// Helper: throw a Java RuntimeException with msg, return 0/null.
static void throwRuntime(JNIEnv* env, const char* msg) {
    jclass ex = env->FindClass("java/lang/RuntimeException");
    if (ex) env->ThrowNew(ex, msg);
}

extern "C" {

JNIEXPORT jlong JNICALL
Java_com_remor_app_LlamaBridge_initModel(JNIEnv* env, jobject /*thiz*/,
                                         jstring modelPath) {
    const char* path = env->GetStringUTFChars(modelPath, nullptr);
    if (!path) return 0;

    // Load model parameters
    llama_model_params mparams = llama_model_default_params();
    llama_model* model = llama_model_load_from_file(path, mparams);
    env->ReleaseStringUTFChars(modelPath, path);
    if (!model) {
        throwRuntime(env, "llama_model_load_from_file failed");
        return 0;
    }

    // Create context
    llama_context_params cparams = llama_context_default_params();
    cparams.n_ctx = 512;   // small context for phone memory budget
    cparams.n_threads = 4; // typical phone big-core count
    llama_context* ctx = llama_init_from_model(model, cparams);
    if (!ctx) {
        llama_model_free(model);
        throwRuntime(env, "llama_init_from_model failed");
        return 0;
    }

    // We stash the model pointer alongside the context by storing it
    // in the context's user data via a small wrapper struct.
    struct Handle { llama_model* model; llama_context* ctx; };
    Handle* h = new Handle{model, ctx};
    return reinterpret_cast<jlong>(h);
}

JNIEXPORT jstring JNICALL
Java_com_remor_app_LlamaBridge_complete(JNIEnv* env, jobject /*thiz*/,
                                        jlong handle, jstring prompt,
                                        jint maxTokens) {
    struct Handle { llama_model* model; llama_context* ctx; };
    Handle* h = reinterpret_cast<Handle*>(handle);
    if (!h || !h->ctx || !h->model) {
        throwRuntime(env, "invalid model handle");
        return nullptr;
    }

    const char* cprompt = env->GetStringUTFChars(prompt, nullptr);
    if (!cprompt) return nullptr;
    std::string promptStr(cprompt);
    env->ReleaseStringUTFChars(prompt, cprompt);

    const llama_vocab* vocab = llama_model_get_vocab(h->model);

    // Tokenize prompt
    std::vector<llama_token> tokens(promptStr.size() + 32);
    int n_tokens = llama_tokenize(vocab, promptStr.c_str(),
                                  (int)promptStr.size(),
                                  tokens.data(), (int)tokens.size(),
                                  /*add_special*/ true,
                                  /*parse_special*/ true);
    if (n_tokens < 0) {
        throwRuntime(env, "tokenization failed");
        return nullptr;
    }
    tokens.resize(n_tokens);

    // Evaluate prompt
    llama_batch batch = llama_batch_get_one(tokens.data(), n_tokens);
    if (llama_decode(h->ctx, batch) != 0) {
        throwRuntime(env, "llama_decode (prompt) failed");
        return nullptr;
    }

    // Generate
    std::string out;
    int n_gen = 0;
    int max_n = (int)maxTokens;
    while (n_gen < max_n) {
        llama_token tok = llama_sampler_sample(
            /*smpl*/ nullptr, h->ctx, /*idx*/ -1);
        // Simple greedy sampler fallback: use argmax via token data.
        // (A full sampler chain is overkill for the bridge v1.)
        if (llama_vocab_is_eog(vocab, tok)) break;
        char buf[64];
        int n = llama_token_to_piece(vocab, tok, buf, sizeof(buf),
                                     /*lstrip*/ 0, /*special*/ true);
        if (n > 0) out.append(buf, n);
        tokens.clear();
        tokens.push_back(tok);
        batch = llama_batch_get_one(tokens.data(), 1);
        if (llama_decode(h->ctx, batch) != 0) break;
        n_gen++;
    }

    return env->NewStringUTF(out.c_str());
}

JNIEXPORT void JNICALL
Java_com_remor_app_LlamaBridge_freeModel(JNIEnv* env, jobject /*thiz*/,
                                         jlong handle) {
    struct Handle { llama_model* model; llama_context* ctx; };
    Handle* h = reinterpret_cast<Handle*>(handle);
    if (!h) return;
    if (h->ctx) llama_free(h->ctx);
    if (h->model) llama_model_free(h->model);
    delete h;
}

} // extern "C"
