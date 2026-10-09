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
import threading
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


# --- ACQ-STT-1: governed faster-whisper substrate ---------------------------
# Acquired 2026-10-08 through the governed external-acquisition loop
# (faster-whisper 1.2.1, PyPI, MIT; CTranslate2 checkpoint
# Systran/faster-whisper-base at revision ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66,
# license mit). The model directory lives outside the git tree; the runtime
# resolves it via REMOR_STT_MODEL_DIR or the default below, and fails closed
# (CapabilityUnavailable, never a fabricated transcript) when absent.

_STT_MODEL_DIR_ENV = "REMOR_STT_MODEL_DIR"
_STT_DEFAULT_MODEL_DIR = os.path.expanduser(
    "~/workspace/models/faster-whisper-base")
_STT_MODEL_ID = "Systran/faster-whisper-base"
_STT_MODEL_REVISION = "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"

_stt_lock = threading.Lock()
_stt_model: Any = None


def _stt_model_dir() -> str:
    return os.environ.get(_STT_MODEL_DIR_ENV, _STT_DEFAULT_MODEL_DIR)


def _load_stt_model() -> Any:
    """Load the governed faster-whisper substrate (process-cached).

    Raises ImportError when faster-whisper is not installed and
    FileNotFoundError when the verified model directory is absent —
    both surface as honest CapabilityUnavailable from transcribe().
    """
    global _stt_model
    with _stt_lock:
        if _stt_model is None:
            from faster_whisper import WhisperModel  # noqa: F401
            model_dir = _stt_model_dir()
            if not os.path.isdir(model_dir):
                raise FileNotFoundError(
                    "STT model dir missing: %s" % model_dir)
            _stt_model = WhisperModel(
                model_dir, device="cpu", compute_type="int8")
        return _stt_model


def _probe_stt_available() -> Tuple[bool, str, List[str]]:
    """Truthful STT availability: the substrate must actually load."""
    try:
        _load_stt_model()
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return (False,
                "STT substrate unavailable (%s: %s)"
                % (type(exc).__name__, exc),
                _probe_stt_substrate())
    return True, "faster-whisper substrate loaded and ready", []


def transcribe(audio: Any = None, *, source: str = "microphone",
               language: str = "en") -> Dict[str, Any]:
    """Speech-to-text via the governed faster-whisper substrate.

    Returns {"ok": True, "transcript": ...} when the substrate is acquired.
    Raises CapabilityUnavailable — never a fabricated transcript — when the
    substrate cannot be loaded, or when no audio is given (the microphone
    input path is unproven on bench: no /dev/snd).

    audio: file path, bytes, file-like object, or numpy array (16 kHz mono
    preferred; faster-whisper resamples). Uses vad_filter=True so silence
    does not hallucinate (proven in the ACQ-STT-1 WER battery).
    """
    if audio is None:
        missing = _probe_stt_substrate()
        raise CapabilityUnavailable(
            capability="voice.speech_to_text",
            reason=("no audio input provided and no microphone path; "
                    "refusing to fabricate a transcript"),
            missing=missing)
    try:
        model = _load_stt_model()
    except Exception as exc:  # noqa: BLE001 - fail closed, honestly
        missing = _probe_stt_substrate()
        raise CapabilityUnavailable(
            capability="voice.speech_to_text",
            reason=("no speech-recognition machinery available "
                    "(%s); refusing to fabricate a transcript"
                    % type(exc).__name__),
            missing=missing) from exc
    # Normalize bytes -> temp file: faster-whisper's in-memory av decode
    # path is av-version fragile (av 14.1.0 rejects its BytesIO wrapper);
    # a real file decodes stably across versions.
    tmp = None
    try:
        if isinstance(audio, (bytes, bytearray, memoryview)):
            fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="remor_stt_")
            with os.fdopen(fd, "wb") as fh:
                fh.write(bytes(audio))
            audio = tmp
        segments, info = model.transcribe(
            audio, language=language, beam_size=5, vad_filter=True)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    transcript = " ".join(s.text for s in segments).strip()
    return {
        "ok": True,
        "transcript": transcript,
        "language": info.language,
        "language_probability": round(info.language_probability, 4),
        "engine": "faster-whisper",
        "model": "%s@%s" % (_STT_MODEL_ID, _STT_MODEL_REVISION[:12]),
    }


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
    stt_ok, stt_detail, stt_missing = _probe_stt_available()
    stt: Dict[str, Any] = {
        "available": bool(stt_ok),
        "classification": "PROVEN" if stt_ok else "UNAVAILABLE",
        "engine": "faster-whisper (CTranslate2), offline",
        "model": "%s@%s" % (_STT_MODEL_ID, _STT_MODEL_REVISION[:12]),
        "probe": stt_detail,
    }
    if not stt_ok:
        stt["missing"] = stt_missing
    return {
        "speech_to_text": stt,
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
