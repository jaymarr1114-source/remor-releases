"""REMOR media substrate: offline neural text-to-speech.

Engine decision (verified on this machine, 2026-09-26):
    Piper (VITS neural TTS, ``piper-tts`` 1.8.0 + onnxruntime) WON.
    Voice: ``en_US-lessac-medium`` (63 MB ONNX, bundled under voices/),
    downloaded from the HuggingFace mirror of the piper-voices collection
    (the old github.com/rhasspy/piper-voices release URLs now 404; the
    collection moved to huggingface.co/rhasspy/piper-voices).

    The espeak-ng fallback was NOT needed: piper installed and ran cleanly
    on 2 CPUs with no GPU and no network access after the voice download.
    (piper-tts wheels bundle piper-phonemize with its own espeak-ng data,
    so no apt install was required.)

Honest bounds:
    * Naturalness: VITS "medium" quality. Clearly intelligible,
      human-voice-like (harmonic pitch structure, natural formants), but
      prosody is flat on long paragraphs and it is recognizably synthetic
      on close listening. It is NOT a beep / pure tone / noise.
    * Latency on 2 CPUs: first call pays ~8 s model+phonemizer load (paid
      once per process; the loaded voice is cached behind a lock). Steady
      state synthesis runs at RTF ~0.5 (1 s of CPU per 2 s of speech).
    * Voice inventory: exactly one bundled voice, en_US-lessac-medium
      (female US-English "lessac" speaker, 22 050 Hz mono). The
      ``voices/`` directory can hold more .onnx + .onnx.json pairs from
      the rhasspy/piper-voices HuggingFace repo; add an entry to VOICES
      to expose them. No voice cloning, no SSML, no speaker control.
    * Language coverage: English (US) only. The phonemizer is espeak-ng,
      so non-English text is best-effort and will usually mispronounce;
      non-ASCII input never crashes (it is ASCII-folded on retry).
    * Determinism: NOT byte-deterministic, by model design. Piper's VITS
      stochastic duration predictor samples timing noise *inside* the ONNX
      graph (no seed is exposed through the API), so two runs of the same
      text differ slightly in phoneme timing (measured ~2% length variation
      on this machine: 117248 vs 120320 vs 115200 bytes for one sentence).
      Same words, same voice, same quality class; only timing/emphasis
      varies run to run. Callers must not checksum outputs.
    * Offline: fully offline after install; no credentials, no cloud.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
import wave

ENGINE_NAME = "piper"
ENGINE_VERSION = "piper-tts 1.8.0 / onnxruntime"

_VOICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices")
VOICES = {
    "default": "en_US-lessac-medium",
    "en_US-lessac-medium": "en_US-lessac-medium",
}

SAMPLE_WIDTH_BYTES = 2          # 16-bit PCM
MAX_TEXT_CHARS = 100_000        # hard cap: refuse beyond this (clean refusal)
CHUNK_CHARS = 350               # pack sentences into chunks of at most this size

_voice_lock = threading.Lock()
_voice_cache: dict[str, object] = {}


def _resolve_voice(voice: str) -> str:
    if not isinstance(voice, str) or voice not in VOICES:
        raise ValueError(
            "unknown voice %r; available voices: %s"
            % (voice, sorted(VOICES))
        )
    return VOICES[voice]


def _voice_path(model_name: str) -> str:
    path = os.path.join(_VOICE_DIR, model_name + ".onnx")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            "voice model missing: %s (expected bundled under voices/)" % path
        )
    return path


def _load_voice(model_name: str):
    """Load (and process-cache) a PiperVoice. ~8 s on 2 CPUs, paid once."""
    with _voice_lock:
        if model_name not in _voice_cache:
            from piper import PiperVoice  # local import: keeps module import light

            _voice_cache[model_name] = PiperVoice.load(_voice_path(model_name))
        return _voice_cache[model_name]


_SENT_SPLIT = re.compile(r"(?<=[.!?\u2026])\s+|\n+")


def _chunk_text(text: str) -> list[str]:
    """Split text into synthesis chunks of at most CHUNK_CHARS chars.

    Sentences are packed greedily; a single over-long sentence is split on
    commas/semicolons, then on whitespace as a last resort.
    """
    sentences: list[str] = []
    for piece in _SENT_SPLIT.split(text):
        piece = piece.strip()
        if not piece:
            continue
        while len(piece) > CHUNK_CHARS:
            cut = max(piece.rfind(c, 0, CHUNK_CHARS) for c in (",", ";", ":"))
            if cut <= 0:
                cut = piece.rfind(" ", 0, CHUNK_CHARS)
            if cut <= 0:
                cut = CHUNK_CHARS
            sentences.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            sentences.append(piece)

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = (current + " " + sentence).strip()
        if len(candidate) <= CHUNK_CHARS:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def _ascii_fold(text: str) -> str:
    return (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
    )


def _synthesize_chunk(voice, chunk: str) -> bytes:
    """Synthesize one chunk to raw int16 bytes; ASCII-fallback on failure."""
    last_error: Exception | None = None
    for attempt in (chunk, _ascii_fold(chunk)):
        if not attempt.strip():
            continue
        try:
            parts: list[bytes] = []
            for result in voice.synthesize(attempt):
                parts.append(result.audio_int16_bytes)
            audio = b"".join(parts)
            if audio:
                return audio
            last_error = RuntimeError("engine produced no audio for chunk")
        except Exception as exc:  # noqa: BLE001 - best effort, never crash
            last_error = exc
    raise RuntimeError("synthesis failed for chunk: %s" % (last_error,))


def synthesize(text: str, out_path: str, voice: str = "default") -> dict:
    """Synthesize *text* to a real WAV file of spoken speech.

    Returns {"ok": True, "out_path", "duration_s", "sample_rate",
    "n_samples", "engine", "voice"} on success, or
    {"ok": False, "error": ...} on clean refusal / failure. On failure no
    output file is left behind.
    """
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "empty text: nothing to synthesize (no file written)"}
    if len(text) > MAX_TEXT_CHARS:
        return {
            "ok": False,
            "error": "text too long (%d chars > %d char cap)" % (len(text), MAX_TEXT_CHARS),
        }
    try:
        model_name = _resolve_voice(voice)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        piper_voice = _load_voice(model_name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "failed to load voice %r: %s" % (model_name, exc)}

    sample_rate = int(piper_voice.config.sample_rate)
    chunks = _chunk_text(text.strip())

    try:
        audio = b"".join(_synthesize_chunk(piper_voice, c) for c in chunks)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if not audio:
        return {"ok": False, "error": "engine produced no audio"}

    try:
        parent = os.path.dirname(os.path.abspath(out_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with wave.open(out_path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(SAMPLE_WIDTH_BYTES)
            wav.setframerate(sample_rate)
            wav.writeframes(audio)
    except Exception as exc:  # noqa: BLE001
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
        except OSError:
            pass
        return {"ok": False, "error": "failed to write WAV: %s" % (exc,)}

    n_samples = len(audio) // SAMPLE_WIDTH_BYTES
    return {
        "ok": True,
        "out_path": os.path.abspath(out_path),
        "duration_s": n_samples / sample_rate,
        "sample_rate": sample_rate,
        "n_samples": n_samples,
        "engine": "%s (%s)" % (ENGINE_NAME, ENGINE_VERSION),
        "voice": model_name,
    }
