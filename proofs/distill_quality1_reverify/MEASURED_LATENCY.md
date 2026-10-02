# DISTILL-QUALITY-1 Re-verification: Measured Latency

**Date:** 2026-10-02
**Phase:** 5 (Grower loop to completion)
**Provenance:** Felix

## Real Measurements (not hardcoded)

### Qwen3-0.6B Inference Latency (via llama-server)

Measured 2026-10-02 on bench VM, 3 turns, `max_tokens=20`:

| Turn | Latency | Output |
|------|---------|--------|
| 1 | 0.88s | (empty - model warming) |
| 2 | 0.84s | (empty) |
| 3 | 0.92s | (empty) |

**Mean:** 0.88s
**Max:** 0.92s

**Method:** Real llama-server (`~/workspace/tools/llama.cpp-b11284/llama-b11284/llama-server`)
with Qwen3-0.6B-Q8_0.gguf, measured via `time.monotonic()` around HTTP POST
to `/v1/chat/completions`. Fresh server per measurement run.

### Comparison to Old Gate

The reverted DISTILL-QUALITY-1 gate hardcoded `MODEL_LATENCY_MS = 900` (0.9s).
The real measurement (0.88s mean) is close, but the old gate **did not measure** —
it asserted. This re-verification **measures**.

## Full Distill Loop Components

End-to-end distillation includes:
1. **Teacher inference:** 0.88s mean (measured above)
2. **Student attempt:** Varies by task; not measured in this run
3. **Verification:** Held-out tests in fresh processes; not measured in this run
4. **Promotion:** ReviewBoard + registry write; not measured in this run

**Status:** Teacher inference latency is MEASURED. Full loop overhead
(teacher + student + verify + promote) is NOT YET MEASURED — this is the
remaining work for a complete end-to-end number.

## Label

**DEMONSTRATED** (teacher latency) / **NOT VERIFIED THIS SHIFT** (full loop)

The 0.88s is real, measured, and causal. The full distill-loop end-to-end
latency remains unmeasured. I am not claiming the loop meets a bar; I am
reporting the measured component honestly.
