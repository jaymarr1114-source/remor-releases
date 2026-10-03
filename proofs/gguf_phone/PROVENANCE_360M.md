# SmolLM2-360M Q8_0 Provenance Record
**Date:** 2026-10-03
**Verified by:** Felix (independent audit)
**Approved by:** James

## Source Chain
1. **Base model:** `HuggingFaceTB/SmolLM2-360M-Instruct`
   - License: Apache 2.0 (verified via HuggingFace API)
   - Author: HuggingFaceTB

2. **Quantization:** `Felladrin/gguf-Q8_0-SmolLM2-360M-Instruct`
   - Quantizer: Felladrin (Victor Nogueira)
   - Method: Mechanical quantization to Q8_0
   - Tool: gguf-trainer or equivalent (Felladrin's native Deno/WebGPU tool)
   - Felladrin's licensing: MIT/Apache-2.0 (per his GitHub profile)

3. **Our verification (2026-10-03):**
   - ✅ Valid GGUF v3 header (magic `GGUF`)
   - ✅ Metadata: architecture `llama`, name `SmolLM2 360M`
   - ✅ Metadata license: `apache-2.0`
   - ✅ Size: 386,404,992 bytes (369MB)
   - ✅ SHA256: `48ab3034d0dd401fbc721eb1df3217902fee7dab9078992d66431f09b7750201`
   - ✅ Tensor structure intact

## License Analysis
- Base model is Apache 2.0 (permits commercial use, modification, redistribution)
- Quantization is a mechanical transformation (no new copyrightable work)
- Felladrin's work is MIT/Apache-2.0 licensed
- GGUF metadata preserves `apache-2.0` license tag
- **Conclusion:** License-clean for bundling in v1.0.12

## Limitations
- We did not perform the quantization ourselves (torch/transformers unavailable at the time)
- We rely on Felladrin's quantization being correct (verified via header/metadata, not by re-quantizing)
- Future work: REMOR self-quantization capability (James's technique idea)

## Bundled In
- v1.0.12: `app/assets/models/smollm2-360m-instruct-q8_0.gguf`
- Bundle SHA256: `d3b820919aa3049c...` (full in .sha256 file)
