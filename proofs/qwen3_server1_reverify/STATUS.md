# QWEN3-SERVER-1 Status: Lifecycle DEMONSTRATED, Integration NOT VERIFIED

**Date:** 2026-10-02
**Phase:** 5 (Grower loop to completion)
**Provenance:** Felix

## What Was Proven (Old Gate, Valid)

The reverted QWEN3-SERVER-1 gate proved:
- llama-server starts (binary exists, port binds)
- Server reaches ready state (health check OK)
- Serves 3 turns (real inference, 0.6B model)
- Max latency 0.60s (measured)
- Clean shutdown (process terminates)

These lifecycle facts are **DEMONSTRATED** and remain valid. The server works.

## What Was NOT Proven (Mandate Gap)

Mandate: "Qwen3 server integrated into the production path"

The old gate did NOT prove:
- Server integrated as ChatService provider backend
- Lifecycle managed by production code (not test harness)
- Reuse across missions (server persists, not per-test spawn)
- Fallback behavior (server down → honest refusal, not crash)

## Why Not Proven: Substrate Boundary

Production integration requires the provider wiring (`build_llm_wiring`)
which needs `llama-cli` (not `llama-server`). The `llama-cli` binary is
unavailable on the bench VM.

This is a genuine substrate boundary:
- `llama-server` exists and works (proven)
- `llama-cli` does not exist (verified via `which`, filesystem search)
- The provider wiring specifically requires `llama-cli`, not the server API

**Without `llama-cli`, the ChatService cannot instantiate a real provider.
The production path integration cannot be proven on the bench.**

## Label

**DEMONSTRATED** (server lifecycle) / **NOT VERIFIED THIS SHIFT** (production integration)

The server works. The integration needs the substrate (llama-cli) or a
rewiring to use the server API instead of the CLI.

## Path Forward

1. **Option A:** Obtain/build `llama-cli` for the bench, then integrate
2. **Option B:** Rewire `Qwen3Teacher` to use llama-server HTTP API instead
   of llama-cli subprocess (engineering work)
3. **Option C:** Defer integration to James's hardware where substrate exists

This is not a failure — it's a named boundary with clear options.
