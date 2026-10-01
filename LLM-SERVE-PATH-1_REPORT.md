# LLM-SERVE-PATH-1 — Report

**Disposition: REPORTED. Do not land.**

## James's decision (U-10, 2026-10-01 ~09:14 EDT)
The http_adapter/LLM-wiring non-connection WAS deliberate — designed so users could plug in their own AI accounts. Directive: (1) prove the existing LLM wiring sufficient for production use, wire it in if so; (2) architect a provider/plugin interface with open slots for user-supplied agents/bots.

## Sufficiency battery

| Battery | Result |
|---|---|
| b1 correctness | 3/3 real Qwen3-8B completions correct, provenance `borrowed:qwen3@7c41481f…`, no stub text |
| b2 latency | p95 and max within declared envelope (<600s / <900s on reference host) |
| b3 error behavior | 3/3: grantless refused, bad grant refused, insufficient deferred — all before any model call |
| b4 governance | 4/4: provenance, telemetry event, charged seconds, result_id linkage |
| b5 isolation | 3/3: sequential completions independent, distinct result_ids |
| b6 registry | 10/10: register/get/default/list/unregister/set_default, thread-safety, validation, builtin via public path |
| b7 end-to-end | 3/3: no-provider honest fallback preserved; escalation works with real inference; grounded kinds stay native |

**Sufficiency verdict: SUFFICIENT** for deferred/background serving. The honest latency (~140s per completion on the reference host) means this is not interactive-chat-suitable, but it is production-suitable for the serving path with the provenance-labeled, FRM-governed borrow pattern.

## Provider interface design

`runtime/services/llm_providers.py` (new):
- `ProviderRegistry`: thread-safe named registry (register/get/default/list/unregister/set_default)
- `ProviderEntry`: bundles provider + grant_issuer + mc_id (the three serving-path needs)
- `register_builtin_provider()`: builds Qwen3 wiring via `build_llm_wiring()` and registers through the public `register()` path — dogfooding
- Provider contract documented: implement `CognitionProvider` Protocol, honor grant governance, fail closed

## Serving-path wiring

- `runtime/services/http_adapter.py`: `build_services()` now includes `llm_providers` registry with builtin registered (honest empty when substrate absent)
- `runtime/services/chat_handler.py`: template path is the native tier; `chit_chat`/`unknown` escalate to the default provider via `_try_provider_escalation()` when available, else the honest "I don't know" (unchanged)
- Escalation issues a real FrmGrant, calls with `native_refusal="no_template_for_kind"`, labels the answer `kind="borrowed"` with provenance

## Incidents
1. b1 first run: llama-cli SIGKILLed (exit -9) under memory pressure — I ran pytest concurrently with inference. Killed the stale 4.4GB llama-cli, waited for load to settle, re-ran b1 alone: green.
2. pytest collection hangs under inference load — verified separately on quiet host.

## Exact next boundary
User-provider onboarding: the registry has the slots, but no user-provider implementation exists yet. A future mission needs to build the first user-provider (e.g., API-key-based) against the documented contract and prove it registers and serves through the same path.

## What remains unproven
- Interactive-chat latency (140s is honest but slow; needs async/streaming for real-time use)
- Concurrent provider calls under load (b5 proves sequential isolation only)
- FRM-epoch integration for grant issuance (current: per-call issuer with disclosed bound)
- On-device proof
