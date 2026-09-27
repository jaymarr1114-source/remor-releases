"""Media generation front (§6): image, video, song, and voice entry points.

All four DELEGATE to the verified media substrate
(``swarm_engine.media.image`` / ``.video`` / ``.song`` / ``.voice``). The
HTTP adapter routes behind this front through the admitted media
capabilities (governed dispatch); direct calls are for in-process use.

``status()`` probes each medium with a genuine minimal generation
through the real substrate path and reports available=True only when
the substrate actually delivered -- never hardcoded.

Honest bounds (kept in every response):
  - Image: seeded PROCEDURAL GENERATIVE ART, not a learned text-to-image
    model. The prompt steers palette/composition parameters only; it
    cannot depict specific people, objects, animals, text, or scenes.
    Same (prompt, seed) -> byte-identical PNG. Cap 2048x2048.
  - Video: procedural ANIMATION (numpy/PIL frames piped to ffmpeg/libx264),
    not learned video synthesis. Abstract motion only; no depiction of
    people/objects/text. Requires ffmpeg on PATH. Caps: (0, 30] s,
    <=1280x720, fps <= 60.
  - Song: additive synthesis -- instrumental bed + TTS vocal, fixed gains,
    one envelope-follower ducker, peak normalization. No EQ, compression,
    reverb, or loudness targeting. 44.1 kHz stereo WAV.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

IMAGE_BOUNDS = [
    "procedural generative art, NOT a learned text-to-image model: "
    "no diffusion network, no neural weights, no training data, no GPU",
    "the prompt steers palette/composition parameters only; it cannot "
    "depict specific people, objects, animals, text, or scenes",
    "deterministic: same (prompt, seed) -> byte-identical PNG",
    "resolution cap 2048x2048 per side",
]

VIDEO_BOUNDS = [
    "procedural animation (numpy/PIL frames encoded with ffmpeg/libx264), "
    "NOT learned video synthesis: abstract motion only",
    "cannot depict specific people, objects, scenes, or text",
    "requires ffmpeg on PATH",
    "caps: duration (0, 30] s, resolution <= 1280x720, fps <= 60",
    "deterministic for (prompt, seed, dims, fps, duration) at the decoded-"
    "frame level",
]

SONG_BOUNDS = [
    "additive song synthesis: instrumental bed + TTS vocal, mixed with "
    "fixed gains, one envelope-follower ducker, peak normalization",
    "no EQ, compression, reverb, de-essing, or loudness targeting; "
    "vocal starts at t=0, song lasts max(vocal, instrumental)",
    "44.1 kHz stereo WAV; vocal bound by the TTS voice bounds "
    "(one English voice, flat prosody, non-deterministic timing)",
]


VOICE_BOUNDS = [
    "one bundled English voice only (en_US-lessac-medium); "
    "no voice cloning, no SSML, no speaker control",
    "English (US) only; non-English text is best-effort",
    "flat prosody on long paragraphs; recognizably synthetic on close "
    "listening",
    "TTS timing is non-deterministic (VITS duration predictor samples "
    "noise inside the model); never checksum outputs",
    "fully offline after install; no credentials, no cloud",
]


def _scratch(name: str, ext: str) -> str:
    return os.path.join(
        tempfile.gettempdir(), f"remor_{name}_{uuid.uuid4().hex[:12]}.{ext}")


def _probe_dir(name: str) -> str:
    d = os.path.join(
        tempfile.gettempdir(), f"remor_probe_{name}_{uuid.uuid4().hex[:12]}")
    os.makedirs(d, exist_ok=False)
    return d


def _cleanup(d: str) -> None:
    shutil.rmtree(d, ignore_errors=True)


def _check_artifact(res: Dict[str, Any], out_path: str) -> Tuple[bool, str]:
    """Common artifact check: substrate said ok AND a non-empty file exists."""
    if not isinstance(res, dict) or res.get("ok") is not True:
        err = res.get("error") if isinstance(res, dict) else repr(res)
        return False, f"substrate refused: {err}"
    if not (os.path.isfile(out_path) and os.path.getsize(out_path) > 0):
        return False, "substrate reported ok but left no output file"
    return True, "probe artifact written and verified"


def _probe_image() -> Tuple[bool, str]:
    """Genuine probe: actually generate a tiny PNG via the real substrate."""
    try:
        from swarm_engine.media.image import generate
    except Exception as exc:
        return False, (f"substrate import failed "
                       f"({type(exc).__name__}: {exc})")
    d = _probe_dir("image")
    try:
        out = os.path.join(d, "probe.png")
        res = generate("remor status probe", out,
                       width=32, height=32, seed=0)
        ok, detail = _check_artifact(res, out)
        return (ok, f"tiny 32x32 PNG probe: {detail}") if ok else (False, detail)
    except Exception as exc:
        return False, f"probe raised {type(exc).__name__}: {exc}"
    finally:
        _cleanup(d)


def _probe_video() -> Tuple[bool, str]:
    """Genuine probe: actually render+encode a tiny clip (exercises ffmpeg)."""
    try:
        from swarm_engine.media.video import generate
    except Exception as exc:
        return False, (f"substrate import failed "
                       f"({type(exc).__name__}: {exc})")
    d = _probe_dir("video")
    try:
        out = os.path.join(d, "probe.mp4")
        res = generate("remor status probe", out, duration_s=1.0, fps=8,
                       width=160, height=90, seed=0)
        ok, detail = _check_artifact(res, out)
        return ((ok, f"tiny 1s clip probe (ffmpeg encode): {detail}")
                if ok else (False, detail))
    except Exception as exc:
        return False, f"probe raised {type(exc).__name__}: {exc}"
    finally:
        _cleanup(d)


def _probe_song() -> Tuple[bool, str]:
    """Genuine probe: actually assemble a minimal song (instrumental +
    TTS vocal + mix) via the real substrate."""
    try:
        from swarm_engine.media.song import assemble_song as _assemble
    except Exception as exc:
        return False, (f"substrate import failed "
                       f"({type(exc).__name__}: {exc})")
    d = _probe_dir("song")
    try:
        out = os.path.join(d, "probe.wav")
        work = os.path.join(d, "work")
        res = _assemble("remor status probe", {"style": "ballad", "bars": 2},
                        out, work)
        ok, detail = _check_artifact(res, out)
        return ((ok, f"minimal 2-bar song probe (instrumental + TTS + mix): "
                 f"{detail}") if ok else (False, detail))
    except Exception as exc:
        return False, f"probe raised {type(exc).__name__}: {exc}"
    finally:
        _cleanup(d)


def _probe_voice() -> Tuple[bool, str]:
    """Genuine probe: actually synthesize a short utterance via the real
    TTS substrate."""
    try:
        from swarm_engine.media.voice import synthesize
    except Exception as exc:
        return False, (f"substrate import failed "
                       f"({type(exc).__name__}: {exc})")
    d = _probe_dir("voice")
    try:
        out = os.path.join(d, "probe.wav")
        res = synthesize("remor status probe", out, voice="default")
        ok, detail = _check_artifact(res, out)
        return (ok, f"short utterance probe: {detail}") if ok else (False, detail)
    except Exception as exc:
        return False, f"probe raised {type(exc).__name__}: {exc}"
    finally:
        _cleanup(d)


def generate_image(prompt: str, *, width: int = 512, height: int = 512,
                   seed: Optional[int] = None,
                   out_path: Optional[str] = None) -> Dict[str, Any]:
    """Text-to-image via the verified substrate (procedural generative
    art). Returns the substrate result dict plus the honest bounds. On
    clean substrate refusal (empty prompt, bad dims), returns
    {"ok": False, "error": ...}."""
    from swarm_engine.media.image import generate

    if out_path is None:
        out_path = _scratch("image", "png")
    res = dict(generate(prompt, out_path, width=width, height=height,
                        seed=seed))
    res["bounds"] = list(IMAGE_BOUNDS)
    return res


def generate_video(prompt: str, *, seconds: float = 4.0, fps: int = 24,
                   width: int = 640, height: int = 360,
                   seed: Optional[int] = None,
                   out_path: Optional[str] = None) -> Dict[str, Any]:
    """Text-to-video via the verified substrate (procedural animation +
    ffmpeg). Returns the substrate result dict plus the honest bounds."""
    from swarm_engine.media.video import generate

    if out_path is None:
        out_path = _scratch("video", "mp4")
    res = dict(generate(prompt, out_path, duration_s=seconds, fps=fps,
                        width=width, height=height, seed=seed))
    res["bounds"] = list(VIDEO_BOUNDS)
    return res


def assemble_song(lyrics: str, spec: Optional[Dict[str, Any]] = None, *,
                  out_path: Optional[str] = None,
                  work_dir: Optional[str] = None) -> Dict[str, Any]:
    """Full-song assembly via the verified substrate (instrumental bed +
    TTS vocal, mixed). spec keys are the music substrate's (tempo_bpm,
    key, chords, bars, style, seed), all optional."""
    from swarm_engine.media.song import assemble_song as _assemble

    if out_path is None:
        out_path = _scratch("song", "wav")
    if work_dir is None:
        work_dir = os.path.join(
            tempfile.gettempdir(), f"remor_songwork_{uuid.uuid4().hex[:12]}")
    res = dict(_assemble(lyrics, spec or {}, out_path, work_dir))
    res["bounds"] = list(SONG_BOUNDS)
    return res


def _status_entry(probe, method: str,
                  bounds: List[str]) -> Dict[str, Any]:
    """Build one status entry from a genuine probe.

    The probe runs the REAL substrate path (a minimal real generation);
    the medium is reported available only if the substrate actually
    delivered an artifact. Any import failure, refusal, exception, or
    missing output file -> available False with the reason recorded.
    Probes never raise: a broken probe is itself evidence of
    unavailability.
    """
    try:
        ok, detail = probe()
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        ok, detail = False, f"probe harness failed: {type(exc).__name__}: {exc}"
    entry: Dict[str, Any] = {
        "available": bool(ok),
        "classification": "PROVEN BUT BOUNDED" if ok else "UNAVAILABLE",
        "method": method,
        "bounds": list(bounds),
        "probe": detail,
    }
    if not ok:
        entry["reason"] = detail
    return entry


def status() -> Dict[str, Any]:
    """Machine-readable media-front status, probed truthfully.

    Each medium runs a genuine minimal generation through the real
    substrate path and is reported available ONLY if the substrate
    actually delivered. A medium the substrate cannot deliver (missing
    dependency, refused generation, broken install) is reported
    available=False with the reason -- never hardcoded True.
    """
    return {
        "generate_image": _status_entry(
            _probe_image, "procedural-generative art", IMAGE_BOUNDS),
        "generate_video": _status_entry(
            _probe_video, "procedural animation + ffmpeg/libx264",
            VIDEO_BOUNDS),
        "assemble_song": _status_entry(
            _probe_song, "additive synthesis (instrumental + TTS vocal)",
            SONG_BOUNDS),
        "synthesize_voice": _status_entry(
            _probe_voice, "offline Piper VITS TTS", VOICE_BOUNDS),
    }
