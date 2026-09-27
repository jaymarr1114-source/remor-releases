"""Full-song assembly for the REMOR media substrate (Artifact lab "full song" card).

assemble_song(lyrics, spec, out_path, work_dir):
  1. renders the instrumental bed via runtime.media.music.synthesize_instrumental
  2. renders the vocal via the voice contract
     runtime.media.voice.synthesize(text, wav_path, voice="default") -> dict
     with keys ok / out_path / duration_s / sample_rate / n_samples
     (the sibling voice worker owns that module; this file only relies on the
     contract above and imports it lazily at call time)
  3. mixes: vocal forward (+2 dB), instrumental ducked ~4 dB under segments
     where the vocal is active (energy-envelope sidechain, smoothed
     attack/release), final peak-normalized 44.1 kHz stereo WAV.

HONEST BOUNDS
  - Mix simplicity: fixed gains, one envelope-follower ducker, peak
    normalization. No EQ, no compression, no reverb, no de-essing, no
    loudness targeting.
  - Vocal placement is deterministic: the vocal starts at t=0 of the mix and
    the song lasts max(vocal, instrumental). No intro/outro arrangement logic.
  - Resampling between stems uses scipy polyphase resampling; both stems are
    expected to be 44.1 kHz already (voice contract should deliver that, but
    the mixer does not depend on it).
  - CPU is dominated by the two synthesis calls; the mix itself is a few
    seconds of numpy per minute of audio.
"""

from __future__ import annotations

import os
import wave
from typing import Dict

import numpy as np
from scipy import signal as spsig

from swarm_engine.media.music import SAMPLE_RATE, synthesize_instrumental


def _synthesize_voice(text: str, wav_path: str, voice: str = "default") -> Dict:
    """Voice contract (owned by the sibling voice worker):

    swarm_engine.media.voice.synthesize(text: str, out_path: str,
                                   voice: str = "default") -> dict
    with keys ok / out_path / duration_s / sample_rate / n_samples.

    Lazily imported so song assembly works as soon as voice.py lands.
    """
    from swarm_engine.media.voice import synthesize  # noqa: E402
    return synthesize(text, wav_path, voice=voice)


def _read_wav_mono(path: str) -> tuple:
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        nch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    elif sw == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width {sw} in {path}")
    data = data.reshape(-1, nch).mean(axis=1)  # mono for analysis
    return data, sr


def _read_wav_stereo(path: str) -> tuple:
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        nch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    elif sw == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width {sw} in {path}")
    data = data.reshape(-1, nch)
    if nch == 1:
        data = np.repeat(data, 2, axis=1)
    return data[:, :2], sr


def _resample_to(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return audio
    g = np.gcd(src_sr, dst_sr)
    up, down = dst_sr // g, src_sr // g
    if audio.ndim == 1:
        return spsig.resample_poly(audio, up, down)
    return np.stack([spsig.resample_poly(audio[:, c], up, down)
                     for c in range(audio.shape[1])], axis=1)


def _vocal_activity_mask(vocal: np.ndarray, sr: int) -> np.ndarray:
    """Smoothed 0..1 mask of where the vocal is active (energy envelope)."""
    frame = max(1, int(0.02 * sr))
    n_frames = int(np.ceil(len(vocal) / frame))
    padded = np.pad(vocal, (0, n_frames * frame - len(vocal)))
    rms = np.sqrt((padded.reshape(n_frames, frame) ** 2).mean(axis=1))
    thr = max(rms.max() * 0.08, 1e-4)
    mask = (rms > thr).astype(np.float64)
    # smooth: fast attack, slow release
    att = np.exp(-1.0 / max(1.0, 0.010 * sr / frame))
    rel = np.exp(-1.0 / max(1.0, 0.250 * sr / frame))
    sm = np.zeros_like(mask)
    v = 0.0
    for i, m in enumerate(mask):
        c = att if m > v else rel
        v = c * v + (1 - c) * m
        sm[i] = v
    return np.repeat(sm, frame)[:len(vocal)]


def _write_wav_stereo(path: str, stereo: np.ndarray, sr: int = SAMPLE_RATE) -> None:
    peak = max(float(np.max(np.abs(stereo))), 1e-9)
    stereo = stereo * (0.89 / peak)
    pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def assemble_song(lyrics: str, spec: Dict, out_path: str, work_dir: str) -> Dict:
    """Render a full song file: instrumental bed + TTS vocal, mixed.

    The vocal is mixed slightly forward (+2 dB) and the instrumental is
    ducked ~4 dB under active vocal segments. Output is a 44.1 kHz stereo
    WAV whose duration is max(vocal, instrumental).
    """
    os.makedirs(work_dir, exist_ok=True)
    inst_path = os.path.join(work_dir, "instrumental.wav")
    vocal_path = os.path.join(work_dir, "vocal.wav")

    inst = synthesize_instrumental(spec, inst_path)
    if not inst.get("ok"):
        return {"ok": False, "error": "instrumental synthesis failed",
                "out_path": out_path}

    v = _synthesize_voice(lyrics, vocal_path)
    if not v.get("ok"):
        return {"ok": False, "error": "voice synthesis failed",
                "out_path": out_path}

    inst_st, inst_sr = _read_wav_stereo(inst_path)
    voc_st, voc_sr = _read_wav_stereo(vocal_path)
    inst_st = _resample_to(inst_st, inst_sr, SAMPLE_RATE)
    voc_st = _resample_to(voc_st, voc_sr, SAMPLE_RATE)

    n = max(len(inst_st), len(voc_st))
    inst_pad = np.zeros((n, 2)); inst_pad[:len(inst_st)] = inst_st
    voc_pad = np.zeros((n, 2)); voc_pad[:len(voc_st)] = voc_st

    mask = _vocal_activity_mask(voc_pad.mean(axis=1), SAMPLE_RATE)
    duck = 1.0 - 0.37 * mask[:, None]          # ~ -4 dB under vocal
    mix = inst_pad * duck * 0.9 + voc_pad * 1.26  # vocal forward (+2 dB)

    _write_wav_stereo(out_path, mix, SAMPLE_RATE)
    return {
        "ok": True,
        "out_path": out_path,
        "duration_s": n / SAMPLE_RATE,
        "sample_rate": SAMPLE_RATE,
        "instrumental_path": inst_path,
        "vocal_path": vocal_path,
        "instrumental_duration_s": inst["duration_s"],
        "vocal_duration_s": v["duration_s"],
    }
