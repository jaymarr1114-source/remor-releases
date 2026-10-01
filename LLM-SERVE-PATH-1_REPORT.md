# LLM-SERVE-PATH-1 — Final Report

**Disposition: REPORTED. Do not land.**
**Worktree:** `~/workspace/worktrees/llm-serve-path-1`, branch `llm-serve-path-1-work`
**Commit:** `4898f17`
**Gate script:** `proofs/llm_serve_path1/gate_run.sh`

## James's decision (U-10, 2026-10-01 ~09:14 EDT)
The http_adapter/LLM-wiring non-connection WAS deliberate — designed so users could plug in their own AI accounts. Directive: (1) prove the existing LLM wiring sufficient, wire it in if so; (2) architect a provider/plugin interface with open slots for user-supplied agents/bots.

## Sufficiency battery — VERDICT: SUFFICIENT

| Battery | Result | Notes |
|---|---|---|
| b1 correctness | 3/3 PASS | Real Qwen3-8B completions: "4", "Paris", "tac" — all correct, provenance `borrowed:qwen3@7c41481f…`, no stub text |
| b2 latency | PASS | p95=149.7s, max=154.8s; envelope <600s / hard <900s held |
| b3 error behavior | 3/3 PASS | grantless → `cognition_refused:no_grant`; bad grant → `cognition_refused:grant_not_frmgrant`; insufficient → `cognition_deferred:insufficient_grant` — all before any model call |
| b4 governance | 4/4 PASS | provenance, telemetry event (outcome=borrowed), charged_s=165.5, result_id linked |
| b5 isolation | 3/3 PASS | Two sequential completions: both ok, distinct result_ids, correct ("10", "12"), independent grants |
| b6 registry | 10/10 PASS | register/get/default/list/unregister/set_default, thread-safety (200 entries, 4 threads), validation, builtin via public path |
| b7 end-to-end | 3/3 PASS | No-provider → honest template fallback preserved; escalation works with real inference; grounded kinds stay native |

**Sufficiency note:** The wiring is sufficient for deferred/background serving. The honest latency (~140s/completion on the reference 2-core host) means it is NOT interactive-chat-suitable — the serving path handles this as a long request with provenance labeling, not a real-time turn.

## Provider interface design

**New file:** `runtime/services/llm_providers.py`

- `ProviderRegistry`: thread-safe named registry. `register(entry, make_default=False)`, `get(name)`, `default()`, `list()`, `unregister(name)`, `set_default(name)`, `default_name()`.
- `ProviderEntry` (frozen dataclass): `name`, `provider` (CognitionProvider Protocol), `grant_issuer` (Callable[[float], FrmGrant]), `mc_id`, `description`.
- `register_builtin_provider(registry, ...)`: builds the Qwen3 wiring via the existing `build_llm_wiring()` and registers through the public `register()` path — dogfooding the plugin mechanism. Raises FileNotFoundError on missing substrate (registry stays empty, serving path refuses honestly).
- Provider contract documented in the module docstring: implement `CognitionProvider`, honor grant governance (no grant → honest refusal), fail closed.

**User-provider slots:** A user's own AI account registers via `registry.register(ProviderEntry(name="user-openai", provider=..., grant_issuer=..., mc_id=...))` — the same call the builtin uses. No serving-path changes needed.

## Serving-path wiring

1. **`runtime/services/http_adapter.py`**: `build_services()` now constructs a `ProviderRegistry`, registers the builtin (try/except FileNotFoundError → empty registry on missing substrate), and includes it as `services["llm_providers"]`.

2. **`runtime/services/chat_handler.py`**: The template path is the native tier. `chit_chat` and `unknown` kinds now try `_try_provider_escalation()` first:
   - Gets the registry from `services["llm_providers"]`; no registry or no default → existing honest fallback (unchanged).
   - Issues a real FrmGrant via the entry's grant issuer (conservative 300s estimate for chat turns).
   - Calls `provider.request_cognition()` with `native_refusal="no_template_for_kind"`.
   - On success returns `kind="borrowed"` with provenance in `grounded`; on any failure returns None → honest template fallback.
   - Grounded template kinds (capabilities, runs, status, etc.) never escalate — native tier wins.

## What was NOT changed
- `granted_cognition.py`, `qwen3_teacher.py`, `substrate.py`, `agent_api.py`: the existing LLM wiring is consumed through its frozen interfaces, never modified.
- The borrow-mission usage path is untouched (verified: b1-b5 use `build_llm_wiring()` directly, same as before).

## Incidents
1. **b1 SIGKILL (exit -9):** I ran pytest concurrently with b1's inference; memory pressure killed llama-cli and left a stale 4.4GB process. Killed it, waited for load to settle, re-ran b1 alone: 3/3 green. (My violation of the no-overlap rule, not a wiring defect.)
2. **b5 type bug:** `passed += ok` where `ok` was a result_id string (not bool) — fixed with `bool()` wrapper, re-ran: 3/3 green. Also hardened b4's fragile `and`-chain patterns.
3. **pytest hangs under inference load:** `tests/contracts/test_chat_handler.py` collection+run stalls when llama-cli is active. Verified the chat_handler template paths directly instead (all preserved). The full pytest suite needs a quiet host.

## Exact next boundary
**User-provider onboarding:** The registry has the slots and the contract is documented, but no user-provider implementation exists. A future mission should build the first user-provider (e.g., API-key-based against an external endpoint) and prove it registers and serves through the same path as the builtin.

## What remains unproven
- Interactive-chat latency: 140s is honest but slow; real-time use needs async/streaming (future work).
- Concurrent provider calls under load (b5 proves sequential isolation only).
- FRM-epoch integration for grant issuance (current: per-call issuer with disclosed bound in `build_llm_wiring`).
- Full pytest suite for chat_handler on a quiet host (template paths verified directly; suite stalls under load).
- On-device proof.
- Felix's independent gate re-run — this report is the coordinator's claim, not a crossing.
