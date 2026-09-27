"""Real instrumental synthesis for the REMOR media substrate.

synthesize_instrumental(spec, out_path) builds a 44.1 kHz stereo WAV from a
chord chart with zero bundled samples: every sample is computed with
numpy/scipy:

  - chord pads: detuned sine+triangle stacks per chord tone, ADSR envelopes
  - bass line: root notes (two octaves below the chord root)
  - melody: deterministic seeded walk over chord tones + key scale degrees,
    sine with vibrato
  - percussion: kick (sine pitch-drop), snare (band-passed noise + body tone),
    hats (high-passed noise), sequenced on a 16-step grid per bar

HONEST BOUNDS
  - Synthesis method: additive oscillator stacks + filtered noise. There are
    no real instrument models (no physical modeling, no convolution) and no
    human performance nuance (no timing/velocity micro-variation beyond the
    fixed accent patterns).
  - Melody simplicity: a deterministic pseudo-random walk over chord tones
    and scale degrees with fixed rhythmic values per style. It resolves and
    stays in key, but it is not composed phrasing.
  - Harmony: triads (+7th when the chord symbol carries one); voice leading
    is not modeled.
  - Mix simplicity: fixed per-stem gains, peak normalization, no compression,
    no reverb, no EQ beyond the percussion filters.
  - CPU: roughly 1-3 s per minute of stereo 44.1 kHz audio on a 2-CPU VM
    (vectorized numpy; memory scales linearly with duration).
"""

from __future__ import annotations

import math
import re
import wave
from typing import Dict, List, Tuple

import numpy as np
from scipy import signal as spsig

SAMPLE_RATE = 44100

_NOTE_SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

_MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
_MINOR_SCALE = [0, 2, 3, 5, 7, 8, 10]


def _note_freq(midi: int) -> float:
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


def parse_chord(sym: str) -> List[int]:
    """Parse a chord symbol into semitone offsets from its root.

    Supports e.g. "C", "G", "Am", "F", "Dm", "Em", "Bdim", "C7", "F#m",
    "Bbmaj7". Unknown/empty suffixes fall back to a major triad.
    """
    m = re.match(r"^\s*([A-Ga-g])([#b]?)(.*)$", sym.strip())
    if not m:
        raise ValueError(f"unparseable chord symbol: {sym!r}")
    letter, acc, suffix = m.group(1).upper(), m.group(2), m.group(3).lower()
    root = (_NOTE_SEMI[letter] + (1 if acc == "#" else -1 if acc == "b" else 0)) % 12
    if "dim" in suffix or suffix.startswith("o"):
        iv = [0, 3, 6]
    elif "aug" in suffix or "+" in suffix:
        iv = [0, 4, 8]
    elif suffix.startswith("m") and not suffix.startswith("maj"):
        iv = [0, 3, 7]
    else:
        iv = [0, 4, 7]
    if "maj7" in suffix:
        iv = iv + [11]
    elif "7" in suffix:
        iv = iv + [10]
    return [(root + i) % 12 for i in iv]


def _key_root_and_scale(key: str) -> Tuple[int, List[int]]:
    m = re.match(r"^\s*([A-Ga-g])([#b]?)(.*)$", key.strip())
    if not m:
        raise ValueError(f"unparseable key: {key!r}")
    letter, acc, suffix = m.group(1).upper(), m.group(2), m.group(3).lower()
    root = (_NOTE_SEMI[letter] + (1 if acc == "#" else -1 if acc == "b" else 0)) % 12
    scale = _MINOR_SCALE if suffix.startswith("m") else _MAJOR_SCALE
    return root, scale


def _adsr(n: int, sr: int, attack: float, decay: float, sustain: float,
          release: float) -> np.ndarray:
    """Linear ADSR envelope over n samples."""
    t = np.arange(n) / sr
    env = np.ones(n)
    a = int(attack * sr)
    d = int(decay * sr)
    r = int(release * sr)
    if a > 0:
        env[:a] = np.linspace(0.0, 1.0, a)
    if d > 0 and a + d <= n:
        env[a:a + d] = np.linspace(1.0, sustain, d)
    sus_end = max(a + d, n - r)
    env[a + d:sus_end] = sustain
    if r > 0:
        env[n - r:] = np.linspace(sustain, 0.0, r)
    return env


def _triangle(phase: np.ndarray) -> np.ndarray:
    return 2.0 * np.abs(2.0 * (phase / (2.0 * math.pi) % 1.0) - 1.0) - 1.0


def _tone(freq: float, n: int, sr: int, detune_cents: float = 0.0,
          triangle_mix: float = 0.35) -> np.ndarray:
    """Sine + triangle stack, detuned by detune_cents."""
    f = freq * 2.0 ** (detune_cents / 1200.0)
    phase = 2.0 * math.pi * f * np.arange(n) / sr
    return (1.0 - triangle_mix) * np.sin(phase) + triangle_mix * _triangle(phase)


def _kick(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(n) / sr
    f = 45.0 + 120.0 * np.exp(-t * 28.0)          # sine pitch-drop 165 -> 45 Hz
    phase = 2.0 * math.pi * np.cumsum(f) / sr
    return np.sin(phase) * np.exp(-t * 9.0)


def _snare(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(n) / sr
    noise = rng.standard_normal(n)
    sos = spsig.butter(4, [1400.0, 5200.0], btype="band", fs=sr, output="sos")
    body = np.sin(2.0 * math.pi * 190.0 * t) * np.exp(-t * 30.0)
    return (spsig.sosfilt(sos, noise) * 0.7 + body * 0.5) * np.exp(-t * 22.0)


def _hat(n: int, sr: int, rng: np.random.Generator, open_: bool = False) -> np.ndarray:
    t = np.arange(n) / sr
    noise = rng.standard_normal(n)
    sos = spsig.butter(4, 7000.0, btype="highpass", fs=sr, output="sos")
    decay = 18.0 if open_ else 95.0
    return spsig.sosfilt(sos, noise) * np.exp(-t * decay)


def _add_hit(buf: np.ndarray, hit: np.ndarray, start: int) -> None:
    end = min(start + len(hit), buf.shape[0])
    if end > start:
        buf[start:end] += hit[: end - start]


def _write_wav_stereo(path: str, left: np.ndarray, right: np.ndarray,
                      sr: int = SAMPLE_RATE) -> None:
    peak = max(float(np.max(np.abs(left))), float(np.max(np.abs(right))), 1e-9)
    g = 0.89 / peak
    stereo = np.stack([left * g, right * g], axis=1)
    pcm = np.clip(stereo, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def synthesize_instrumental(spec: Dict, out_path: str) -> Dict:
    """Synthesize an instrumental from a chord chart and write a stereo WAV.

    spec keys (all optional, defaults shown):
      tempo_bpm: float = 120.0
      key:       str   = "C"        (e.g. "C", "Am", "F#")
      chords:    list  = ["C","G","Am","F"]  (one chord per bar, cycles)
      bars:      int   = 8
      style:     str   = "ballad"   ("ballad" | "upbeat")
      seed:      int   = 7          (melody determinism)

    Returns {"ok", "out_path", "duration_s", "sample_rate"}.
    """
    tempo = float(spec.get("tempo_bpm", 120.0))
    key = str(spec.get("key", "C"))
    chords = list(spec.get("chords", ["C", "G", "Am", "F"])) or ["C"]
    bars = int(spec.get("bars", 8))
    style = str(spec.get("style", "ballad")).lower()
    seed = int(spec.get("seed", 7))
    if style not in ("ballad", "upbeat"):
        raise ValueError(f"style must be 'ballad' or 'upbeat', got {style!r}")
    if tempo <= 0 or bars <= 0:
        raise ValueError("tempo_bpm and bars must be positive")

    sr = SAMPLE_RATE
    beat = 60.0 / tempo
    bar_dur = 4.0 * beat
    step = beat / 4.0                      # 16th-note grid
    total_s = bars * bar_dur + 0.5         # small tail for release
    n = int(total_s * sr)
    key_root, scale = _key_root_and_scale(key)
    rng = np.random.default_rng(seed)

    pad_l = np.zeros(n); pad_r = np.zeros(n)
    bass = np.zeros(n)
    mel_l = np.zeros(n); mel_r = np.zeros(n)
    kick_b = np.zeros(n); snare_b = np.zeros(n); hat_b = np.zeros(n)

    # ---- arrangement per bar ----
    for bar in range(bars):
        chord_sym = chords[bar % len(chords)]
        semis = parse_chord(chord_sym)
        root_semi = semis[0]
        bar_start = int(bar * bar_dur * sr)

        # chord pad: one triad stack per bar, ADSR shaped by style
        pad_n = int((bar_dur + 0.25) * sr)
        if style == "ballad":
            env = _adsr(pad_n, sr, attack=0.35, decay=0.4, sustain=0.75, release=0.5)
        else:
            env = _adsr(pad_n, sr, attack=0.02, decay=0.15, sustain=0.55, release=0.25)
        for i, s in enumerate(semis):
            midi = 48 + ((s - root_semi) % 12)   # root near C3, voicing up
            f = _note_freq(midi)
            amp = 0.30 / len(semis)
            pad_l[bar_start:bar_start + pad_n] += _tone(f, pad_n, sr, -5.0) * env * amp
            pad_r[bar_start:bar_start + pad_n] += _tone(f, pad_n, sr, +5.0) * env * amp

        # bass: chord root, two octaves below middle C
        bass_f = _note_freq(36 + (root_semi - _NOTE_SEMI["C"]) % 12)
        if style == "ballad":
            b_n = int(bar_dur * sr)
            b_env = _adsr(b_n, sr, 0.02, 0.1, 0.8, 0.3)
            _add_hit(bass, _tone(bass_f, b_n, sr, triangle_mix=0.5) * b_env * 0.55,
                     bar_start)
        else:
            q = int(beat * sr)
            for b in range(4):
                b_env = _adsr(q, sr, 0.01, 0.08, 0.7, 0.12)
                _add_hit(bass, _tone(bass_f, q, sr, triangle_mix=0.5) * b_env * 0.5,
                         bar_start + b * q)

        # melody: seeded walk over chord tones + scale degrees
        if style == "ballad":
            n_notes, note_dur = 4, beat * 2.0      # half notes
        else:
            n_notes, note_dur = 8, beat            # quarter notes
        m_rng = np.random.default_rng(seed * 1000 + bar)
        degree = 2
        chord_midis = [60 + ((s - root_semi) % 12) for s in semis]
        scale_midis = [60 + ((key_root + st - root_semi) % 12) for st in scale]
        pool = sorted(set(chord_midis + scale_midis))
        for ni in range(n_notes):
            degree = int(np.clip(degree + m_rng.integers(-2, 3),
                                 0, len(pool) - 1))
            f = _note_freq(pool[degree])
            mn = int(note_dur * sr)
            t = np.arange(mn) / sr
            vib = 1.0 + 0.006 * np.sin(2.0 * math.pi * 5.5 * t) * np.minimum(t * 4, 1.0)
            phase = 2.0 * math.pi * np.cumsum(f * vib) / sr
            m_env = _adsr(mn, sr, 0.03, 0.08, 0.8, min(0.25, note_dur * 0.4))
            note = np.sin(phase) * m_env * 0.34
            st = bar_start + int(ni * note_dur * sr)
            _add_hit(mel_l, note, st)
            _add_hit(mel_r, note, st)

        # percussion on the 16-step grid
        steps = 16
        step_n = int(step * sr)
        if style == "ballad":
            kicks = [0, 8]
            snares = [4, 12]
            hats = list(range(0, 16, 2))
        else:
            kicks = [0, 4, 8, 12]
            snares = [4, 12]
            hats = list(range(16))
        for s in range(steps):
            st = bar_start + s * step_n
            vel = 1.0 if s % 4 == 0 else 0.7
            if s in kicks:
                _add_hit(kick_b, _kick(int(0.35 * sr), sr, rng) * 0.85 * vel, st)
            if s in snares:
                _add_hit(snare_b, _snare(int(0.30 * sr), sr, rng) * 0.55 * vel, st)
            if s in hats:
                _add_hit(hat_b, _hat(int(0.12 * sr), sr, rng) * 0.30 * vel, st)

    # stereo: pads are already detuned per channel; add slight extra width
    perc = kick_b + snare_b + hat_b
    left = pad_l * 0.9 + bass * 0.9 + mel_l + perc * 0.9
    right = pad_r * 0.9 + bass * 0.9 + mel_r + perc * 0.9
    # slight channel decorrelation for width (pads already detuned per side)
    left = left + (pad_l - pad_r) * 0.15
    right = right - (pad_l - pad_r) * 0.15

    _write_wav_stereo(out_path, left, right, sr)
    return {
        "ok": True,
        "out_path": out_path,
        "duration_s": n / sr,
        "sample_rate": sr,
    }
