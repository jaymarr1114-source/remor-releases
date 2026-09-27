"""Voice front (§5): speech-to-text and text-to-speech entry points.

Text-to-speech DELEGATES to the verified media substrate
(``swarm_engine.media.voice``: offline Piper VITS, one bundled English
voice). Speech-to-text stays UNAVAILABLE: no STT machinery exists, and a
transcript is never fabricated.

These entry points exist so callers (HTTP API, GUI backend, agents) have
one governed place to ask for voice. The HTTP adapter routes behind this
front through the admitted media capability (governed dispatch), not
through these direct calls; direct calls are for in-process use.

Honest bounds (kept in every response):
  - Exactly one bundled voice: en_US-lessac-medium (US English, female,
    22050 Hz mono). No voice cloning, no SSML, no speaker control.
  - Language: English (US) only; non-English text is best-effort.
  - Prosody: flat on long paragraphs; recognizably synthetic on close
    listening.
  - Timing is NOT byte-deterministic: the VITS duration predictor samples
    noise inside the model, so repeated runs of the same text differ
    slightly in phoneme timing (~2%). Callers must not checksum outputs.
  - Offline: no credentials, no cloud.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.services.unavailable import CapabilityUnavailable

VOICE_BOUNDS: List[str] = [
    "one bundled English voice only (en_US-lessac-medium); "
    "no voice cloning, no SSML, no speaker control",
    "English (US) only; non-English text is best-effort and will usually "
    "mispronounce",
    "flat prosody on long paragraphs; recognizably synthetic on close "
    "listening",
    "TTS timing is non-deterministic (VITS duration predictor samples "
    "noise inside the model): repeated runs of the same text differ "
    "slightly in phoneme timing; never checksum outputs",
    "fully offline after install; no credentials, no cloud",
]


def _probe_stt_substrate() -> List[str]:
    missing: List[str] = []
    try:
        import pyaudio  # noqa: F401
    except Exception:
        missing.append("pyaudio (microphone input)")
    try:
        import speech_recognition  # noqa: F401
    except Exception:
        missing.append("speech_recognition")
    for mod in ("whisper", "faster_whisper", "vosk"):
        try:
            __import__(mod)
        except Exception:
            missing.append(f"{mod} (local ASR weights)")
    if not os.path.exists("/dev/snd"):
        missing.append("/dev/snd (audio input device)")
    if not shutil.which("arecord"):
        missing.append("arecord")
    return missing


def transcribe(audio: Any = None, *, source: str = "microphone",
               language: str = "en") -> Dict[str, Any]:
    """Speech-to-text. Always raises CapabilityUnavailable: no STT
    machinery exists anywhere in the substrate; a transcript is never
    fabricated."""
    missing = _probe_stt_substrate()
    raise CapabilityUnavailable(
        capability="voice.speech_to_text",
        reason=("no speech-recognition machinery exists; refusing to "
                "fabricate a transcript"),
        missing=missing)


def speak(text: str, *, voice: Optional[str] = None,
          language: str = "en",
          out_path: Optional[str] = None) -> Dict[str, Any]:
    """Text-to-speech via the verified substrate (offline Piper VITS).

    Returns the substrate result (ok/out_path/duration_s/sample_rate/
    n_samples/engine/voice) plus the honest bounds list. On clean
    substrate refusal (empty text, over the char cap, unknown voice),
    returns {"ok": False, "error": ...} -- no file is left behind.

    If out_path is None, audio goes to a scratch file under the system
    temp dir; callers behind the governed HTTP front pass an explicit
    path inside the granted media out-dir instead.
    """
    from swarm_engine.media.voice import synthesize, VOICES

    chosen = voice or "default"
    if out_path is None:
        out_path = os.path.join(
            tempfile.gettempdir(), f"remor_voice_{uuid.uuid4().hex[:12]}.wav")
    res = synthesize(text, out_path, voice=chosen)
    res = dict(res)
    res["bounds"] = list(VOICE_BOUNDS)
    res["available_voices"] = sorted(VOICES)
    return res


def status() -> Dict[str, Any]:
    """Machine-readable voice-front status, probed truthfully.

    text_to_speech runs a genuine minimal synthesis through the real
    TTS substrate (short utterance -> WAV artifact) and is reported
    available ONLY when the substrate actually delivered. Missing
    piper, a refused synthesis, or a missing output file -> available
    False with the reason -- never hardcoded True. Probes never raise:
    a broken probe is itself evidence of unavailability.
    speech_to_text stays UNAVAILABLE: no STT machinery exists.
    """
    try:
        tts_ok, tts_detail = _probe_tts()
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        tts_ok = False
        tts_detail = (f"probe harness failed: {type(exc).__name__}: {exc}")
    tts: Dict[str, Any] = {
        "available": bool(tts_ok),
        "classification": "PROVEN BUT BOUNDED" if tts_ok else "UNAVAILABLE",
        "engine": "piper (VITS neural TTS), offline",
        "voices": ["en_US-lessac-medium"],
        "bounds": list(VOICE_BOUNDS),
        "probe": tts_detail,
    }
    if not tts_ok:
        tts["reason"] = tts_detail
    return {
        "speech_to_text": {
            "available": False, "classification": "UNAVAILABLE",
            "missing": _probe_stt_substrate()},
        "text_to_speech": tts,
    }


def _probe_tts() -> Tuple[bool, str]:
    """Genuine probe: actually synthesize a short utterance via the real
    TTS substrate into a scratch dir; available only if the substrate
    delivered a non-empty artifact."""
    try:
        from swarm_engine.media.voice import synthesize
    except Exception as exc:
        return False, (f"substrate import failed "
                       f"({type(exc).__name__}: {exc})")
    d = os.path.join(
        tempfile.gettempdir(), f"remor_probe_tts_{uuid.uuid4().hex[:12]}")
    try:
        os.makedirs(d, exist_ok=False)
        out = os.path.join(d, "probe.wav")
        res = synthesize("remor status probe", out, voice="default")
        if not isinstance(res, dict) or res.get("ok") is not True:
            err = res.get("error") if isinstance(res, dict) else repr(res)
            return False, f"substrate refused: {err}"
        if not (os.path.isfile(out) and os.path.getsize(out) > 0):
            return False, "substrate reported ok but left no output file"
        return True, "short utterance probe: artifact written and verified"
    except Exception as exc:
        return False, f"probe raised {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(d, ignore_errors=True)
