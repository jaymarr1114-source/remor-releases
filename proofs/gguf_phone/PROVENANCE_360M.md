# SmolLM2-360M Q8_0 Provenance Record
**Date:** 2026-10-03 (corrected — supersedes the earlier Felladrin-chain version, which was wrong)
**Verified by:** Felix (independent audit)
**Approved by:** James

## Source Chain (verified 2026-10-03)
1. **Base model:** `https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct`
   - Revision: `a10cc1512eabd3dde888204e902eca88bddb4951` (API `sha`)
   - License: Apache 2.0 (verified via repo `cardData`)
   - Weight evidence: two 10MB spot-checks byte-identical to the official
     checkpoint (embed region sha256 `c8ae44df…09d1`, mid-layer region
     `1250f30b…1fb6`); `config.json` content-identical; tensor map
     identical (290 tensors, 361,821,120 params, same names/shapes/BF16).

2. **Quantization:** LOCAL self-quant, performed 2026-10-03 by a prior
   agent session (llama.cpp pipeline; "135M + 360M safetensors pulled,
   360M self-quantized to q8_0 GGUF" per that session's tool log).
   - Quantizer unattributed: the GGUF has NO `general.quantized_by` field,
     no recorded tool commit, flags, or command line.
   - NOT Felladrin (the earlier record's quantizer claim was wrong).
   - NOT a Loubnabnl fine-tune: no `loubnabnl` repo matching
     "Smollm2 360M 8k Lc100K Mix1 Ep2" exists on the Hub (all 50 public
     repos enumerated; Hub search for `lc100k`/`mix1`/`8k-lc100k` empty;
     candidate repo names return authenticated 404). The
     `general.organization=Loubnabnl` / `general.finetune=8k-lc100k-mix1-ep2`
     strings are local-quant metadata only. No weight delta from the
     official instruct checkpoint was detectable in any region sampled.

3. **Our verification (2026-10-03):**
   - Valid GGUF v3 header (magic `GGUF`), 290 tensors (not 272)
   - Metadata license: `apache-2.0` (the one metadata claim that checks out)
   - Size: 386,404,992 bytes (369MB)
   - SHA256: `48ab3034d0dd401fbc721eb1df3217902fee7dab9078992d66431f09b7750201`
   - GGUF tail is real weight data (0.8% zeros in last 8MB — not zero-filled)

## Known defects (why this artifact gets replaced, not repaired in place)
- The local `model.safetensors` next to the GGUF is TRUNCATED/corrupt:
  718,331,365 bytes on disk vs 723,674,912 declared by its own header
  (last 5,343,547 bytes unreadable). It cannot be used to re-derive the quant.
- Unattributed quantizer + false "Loubnabnl finetune" metadata strings.
- Queued: clean swap to a documented upstream quant —
  `HuggingFaceTB/SmolLM2-360M-Instruct-GGUF` at a pinned revision —
  after the P6/multipart gate, before any publish. If a local quant is
  ever required instead, record the exact llama.cpp commit, conversion
  flags, quantize type, and set `quantized_by`.

## License Analysis
- Base model is Apache 2.0 (permits commercial use, modification, redistribution)
- Quantization is a mechanical transformation (no new copyrightable work)
- GGUF metadata preserves `apache-2.0` license tag (verified against repo cardData)
- **Conclusion:** License-clean for bundling; the Apache-2.0 claim is verified.
  The defect is attribution/provenance hygiene, not license.

## Bundled In
- v1.0.12–v1.0.15: `app/assets/models/smollm2-360m-instruct-q8_0.gguf`
  (sha256 `48ab3034…7750201`, the local self-quant — see defects above)
