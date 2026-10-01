# AUDIO-1 acquisition record (governed external acquisition)

James's standing rule: real substrates come from governed acquisition
(trusted index, sha256-verified, sandbox-scoped) — never hand-synthesized.
This record covers every acquired component used by the audio pipeline.

## 1. piper-tts 1.8.0 — ACQUIRED (voice synthesis engine)

- **Index (trusted):** PyPI — https://pypi.org/pypi/piper-tts/1.8.0/json
- **Artifact:** `piper_tts-1.8.0-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.manylinux_2_28_x86_64.whl`
- **SHA-256 (PyPI record):** `25b4d3f31ff70c8fa7151908e00aaa5650cbdf16bca8fcf21299f3941b89a7d3`
- **SHA-256 (downloaded, verified 2026-09-30):** `25b4d3f31ff70c8fa7151908e00aaa5650cbdf16bca8fcf21299f3941b89a7d3` — MATCH
- **License (PyPI metadata, verified at this revision — not assumed):** `GPL-3.0-or-later`
- **License note:** copyleft. The engine runs as a local library behind the
  governed voice module; the v12 commercial-path implication of GPL-3.0 is
  James's product call. Recorded here so the decision is made on facts.
- **Installed:** `~/workspace/venvs/audio-1/` (piper-tts 1.8.0, onnxruntime
  1.30.0 as its dependency). Install scope: mission venv only; system
  python untouched.
- **Sandbox scope:** offline after install — no credentials, no cloud, no
  network calls during inference (verified: synthesis ran with no network
  dependency; the engine is pure local ONNX inference).

## 2. Voice model en_US-lessac-medium — PRE-VENDORED (reused, not re-acquired)

- **Location:** `runtime/media/voices/en_US-lessac-medium.onnx` (+ `.onnx.json`)
- **SHA-256 (local file, 2026-09-30):** `5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f`
- **Recorded source (2026-09-26 engine decision):** HuggingFace mirror of the
  piper-voices collection (`rhasspy/piper-voices`); the old
  github.com/rhasspy/piper-voices release URLs 404'd, collection moved to HF.
- **Upstream hash re-verification:** NOT re-performed this mission (the file
  was vendored 2026-09-26 and its provenance recorded then); the local hash
  above is the pin going forward. No-duplication mandate: reuse, don't
  re-download.
- **Functional proof:** renders real speech (3.22 s / 22 050 Hz / 70 912
  samples for a 46-char sentence; peak 32767; 94% nonzero) — measured
  2026-09-30 on this host.

## 3. ACE-Step (instrumental music generation) — NOT ACQUIRED (bounded gap)

James's decided instrumental substrate. Assessed honestly on 2026-09-30:

- **Repo:** `ACE-Step/acestep-v15-xl-turbo` (HuggingFace); card license: MIT
  (would have been license-clean).
- **Size:** 4 shards; shard 1 alone = 4,986,971,456 bytes (~4.99 GB, measured
  via ranged GET Content-Range `bytes 0-0/4986971456`). Estimated total
  ~15–20 GB.
- **Why not acquired:** this host has 7 GB RAM total (~1 GB free) and 2 CPUs,
  no GPU. The weights cannot load into RAM, and diffusion-transformer
  inference on 2 CPU cores is not viable for the product path. Genuinely
  unavailable resource on this host (terminal condition b).
- **Honest consequence:** the instrumental side of the pipeline is the
  existing algorithmic synthesis engine (`runtime/media/music.py`) — real
  computed audio with documented bounds, labeled as such in every response.
  It is NEVER passed off as ACE-Step. ACE-Step acquisition is re-attempted
  when GPU-class hardware is available; the mix seam takes stems, so the
  upgrade is a drop-in.

## 4. numpy / scipy — supporting numerical substrate

- Installed in the mission venv from PyPI (trusted index): numpy 2.5.3,
  scipy 1.18.1. Used only for DSP (resampling, envelopes, filters) — the
  audio itself is computed, not sampled from these packages.
- The default system python carries numpy 1.26.4 / scipy 1.11.4; the mix
  route (no piper needed) runs there too.

## What was NOT hand-synthesized

Voice audio: piper VITS neural TTS (acquired, §1) + vendored voice model
(§2). Instrumental audio: pre-existing canonical module reused in place
(per the no-duplication mandate), honestly labeled. No oscillator was
written by this mission; no API was called; no placeholder bytes were
generated anywhere in the pipeline.
