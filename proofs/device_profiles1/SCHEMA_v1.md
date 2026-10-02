# Device Capability Profile — Schema v1 (FROZEN 2026-10-02)

A profile is the attested capability record for a device class. Profiles are
**measured, not declared** — every number traces to a measurement run.

## Fields

| Field | Type | Required | Description |
|---|---|---|---|
| device_class | string | yes | phone \| tablet \| laptop \| desktop_gpu |
| soc | string | yes | SoC/chip identifier + source |
| ram_gb | number | yes | RAM in GB |
| cpu_cores | number | yes | Usable CPU cores for inference |
| cpu_arch | string | yes | arm64 \| x86_64 |
| gpu | string | no | GPU identifier, or "none" |
| npu | string | yes | NPU verdict: AVAILABLE \| UNAVAILABLE + probe ref |
| tier_model | string | yes | Max model per QWEN3_TIER_TABLE.md |
| tier_quant | string | yes | Quantization |
| tier_ctx | number | yes | Max context length |
| measured_latency_mean_s | number | yes | Mean per-turn latency (measured) |
| measured_latency_max_s | number | yes | Max per-turn latency (measured) |
| measured_cold_start_s | number | yes | Cold start seconds (measured) |
| measured_tokens_per_s | string | yes | Token throughput range (measured) |
| thermal_note | string | yes | Thermal constraints |
| measurement_ref | string | yes | Report/proof path for the numbers |
| measured_at | string | yes | ISO timestamp |
| license | string | yes | Must be Apache-2.0 (standing) |

## Rules
- No field estimated. Every number has a measurement_ref.
- Proxy data must be named as proxy with the gap explicitly stated.
- License is always Apache-2.0. No GPL anywhere.
