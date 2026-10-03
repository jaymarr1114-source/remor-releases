# SmolLM2-360M Q8_0 Provenance Record
**Date:** 2026-10-03 (corrected twice — see §4 for the full correction history)
**Verified by:** Felix (independent audit)
**Approved by:** James

## Source Chain (verified 2026-10-03)
1. **Base model:** `https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct`
   - Revision: `a10cc1512eabd3dde888204e902eca88bddb4951` (API `sha`)
   - License: Apache 2.0 (verified via repo `cardData`)
   - Weight evidence: two 10MB spot-checks byte-identical to the official
     checkpoint; `config.json` content-identical; tensor map identical
     (290 tensors, 361,821,120 params, same names/shapes/BF16).

2. **Artifact:** `https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct-GGUF`
   - File: `smollm2-360m-instruct-q8_0.gguf`
   - Revision: `593b5a2e04c8f3e4ee880263f93e0bd2901ad47f` (API `sha`, pinned)
   - License: Apache 2.0 (verified via repo `cardData`)
   - This is HuggingFaceTB's OFFICIAL GGUF publish — the quantizer is
     attributed (HuggingFaceTB's conversion pipeline for this repo).
   - **The local file is byte-identical to this upstream artifact**
     (verified 2026-10-03: fresh 386,404,992-byte download from the pinned
     revision + `cmp` against the local file — identical; sha256
     `48ab3034d0dd401fbc721eb1df3217902fee7dab9078992d66431f09b7750201`).
   - The `general.organization=Loubnabnl` /
     `general.finetune=8k-lc100k-mix1-ep2` strings are UPSTREAM metadata
     carried in HuggingFaceTB's official file (confirmed by direct HTTP
     Range fetch of the file head from huggingface.co — the strings are in
     the official bytes, not local fabrication). Which upstream checkpoint
     HuggingFaceTB's conversion labeled this way is not documented in the
     repo; the artifact is their official publish regardless.

3. **Our verification (2026-10-03):**
   - Valid GGUF v3 header (magic `GGUF`), 290 tensors
   - Metadata license: `apache-2.0` (matches repo cardData)
   - Size: 386,404,992 bytes (369MB)
   - SHA256: `48ab3034d0dd401fbc721eb1df3217902fee7dab9078992d66431f09b7750201`
   - GGUF tail is real weight data (0.8% zeros in last 8MB — not zero-filled)

## Correction history (what was wrong before)
- v1 (wrong): claimed a Felladrin quantizer chain. Retracted — no evidence.
- v2 (wrong): claimed LOCAL self-quant by a prior session with unattributed
  quantizer, and that the Loubnabnl strings were local conversion metadata.
  Retracted 2026-10-03: the prior session's tool log ("self-quantized")
  was wrong — no local quantization occurred (consistent with the local
  `model.safetensors` being truncated: it could never have been converted).
  The file was always the official HuggingFaceTB GGUF download.
- v3 (this record): official upstream artifact, byte-identical, pinned
  revision. No swap needed — the "queued clean swap" is CLOSED as
  unnecessary. The truncated local `model.safetensors` is irrelevant
  (nothing needs re-deriving) and remains quarantined, not used.

## License Analysis
- Base model is Apache 2.0 (permits commercial use, modification, redistribution)
- The GGUF is HuggingFaceTB's official Apache-2.0 publish of the same
  lineage (quantization is a mechanical transformation; repo cardData
  confirms Apache 2.0)
- GGUF metadata preserves the `apache-2.0` license tag (verified)
- **Conclusion:** License-clean for bundling. Provenance is now
  fully attributed: HuggingFaceTB official weights → HuggingFaceTB
  official GGUF, both pinned, both Apache-2.0.

## Bundled In
- v1.0.12–v1.0.15: `app/assets/models/smollm2-360m-instruct-q8_0.gguf`
  (sha256 `48ab3034…7750201` — the official HuggingFaceTB GGUF,
  previously mislabeled as a local self-quant)
