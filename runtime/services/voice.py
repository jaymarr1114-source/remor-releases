"""Voice front (§5): speech-to-text and text-to-speech entry points.

Classification: UNAVAILABLE. There is no ASR substrate, no TTS
synthesizer, no audio input/output device, and no provider credentials
in this environment (audited 2026-09-25: no pyaudio, speech_recognition,
whisper/faster-whisper/vosk, /dev/snd, arecord/aplay; no pyttsx3, gTTS,
edge-tts, Coqui, Bark, espeak/espeak-ng, festival; no audio device).

These entry points exist so callers (HTTP API, GUI backend, agents) have
one governed place to ask for voice -- and get an honest, specific
refusal instead of silence or a fabricated transcript. The substrate
probe runs at call time so the refusal always describes the live
environment, and so a future environment that genuinely gains a
substrate can be wired here without changing callers.
"""
from __future__ import annotations

import shutil
from typing import Any, Dict, List, Optional

from swarm_engine.services.unavailable import CapabilityUnavailable


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
    import os
    if not os.path.exists("/dev/snd"):
        missing.append("/dev/snd (audio input device)")
    if not shutil.which("arecord"):
        missing.append("arecord")
    return missing


def _probe_tts_substrate() -> List[str]:
    missing: List[str] = []
    for mod in ("pyttsx3", "gtts", "edge_tts"):
        try:
            __import__(mod)
        except Exception:
            missing.append(f"{mod} (TTS synthesizer)")
    for bin_ in ("espeak", "espeak-ng", "festival"):
        if not shutil.which(bin_):
            missing.append(bin_)
    import os
    if not os.path.exists("/dev/snd"):
        missing.append("/dev/snd (audio output device)")
    return missing


def transcribe(audio: Any = None, *, source: str = "microphone",
               language: str = "en") -> Dict[str, Any]:
    """Speech-to-text. Always raises CapabilityUnavailable: no ASR substrate."""
    missing = _probe_stt_substrate()
    raise CapabilityUnavailable(
        capability="voice.speech_to_text",
        reason=("no speech-recognition substrate in this environment; "
                "refusing to fabricate a transcript"),
        missing=missing)


def speak(text: str, *, voice: Optional[str] = None,
          language: str = "en") -> Dict[str, Any]:
    """Text-to-speech. Always raises CapabilityUnavailable: no synthesizer."""
    missing = _probe_tts_substrate()
    raise CapabilityUnavailable(
        capability="voice.text_to_speech",
        reason=("no speech-synthesis substrate in this environment; "
                "refusing to pretend audio was produced"),
        missing=missing)


def status() -> Dict[str, Any]:
    """Machine-readable voice-front status: unavailable + exact gaps."""
    return {
        "speech_to_text": {
            "available": False, "classification": "UNAVAILABLE",
            "missing": _probe_stt_substrate()},
        "text_to_speech": {
            "available": False, "classification": "UNAVAILABLE",
            "missing": _probe_tts_substrate()},
    }
