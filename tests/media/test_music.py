"""Tests for the REMOR media music substrate (worker B: music/song).

Covers:
  - synthesize_instrumental: valid WAV, duration, stereo, non-silent,
    harmonic peaks at chord frequencies, percussion onsets.
  - assemble_song with a clearly-labeled TEST FIXTURE vocal (NOT TTS output;
    synthesized numpy tones, used only to exercise the mixer/assembly code
    path). The assembly code path is identical for the real voice module:
    the test monkeypatches only the voice-contract call boundary.
"""

import os
import sys
import wave

import numpy as np
import pytest
from scipy import signal as spsig
from scipy.io import wavfile

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.media import song as song_mod  # noqa: E402
from swarm_engine.media.music import SAMPLE_RATE, synthesize_instrumental  # noqa: E402
from swarm_engine.media.song import assemble_song  # noqa: E402

FIXTURE_NAME = "test_fixture_vocal_do_not_ship.wav"  # never presented as TTS


def _read_pcm(path):
    with wave.open(path, "rb") as w:
        assert w.getnchannels() == 2, "expected stereo"
        assert w.getsampwidth() == 2
        assert w.getframerate() == SAMPLE_RATE
        n = w.getnframes()
        raw = w.readframes(n)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    return pcm.reshape(-1, 2), n / SAMPLE_RATE


def make_test_fixture_vocal(path, duration_s=3.0):
    """Synthesize a clearly-labeled TEST FIXTURE vocal (not TTS): a sung-like
    phrase of vibrato tones with harmonics and pauses between 'words'."""
    sr = SAMPLE_RATE
    n = int(duration_s * sr)
    buf = np.zeros(n)
    # phrase: (freq_hz, start_s, len_s)
    phrase = [(392.0, 0.1, 0.5), (440.0, 0.75, 0.5), (523.25, 1.4, 0.6),
              (440.0, 2.15, 0.55)]
    for f, start, length in phrase:
        m = int(length * sr)
        t = np.arange(m) / sr
        vib = 1.0 + 0.008 * np.sin(2 * np.pi * 5.0 * t)
        ph = 2 * np.pi * np.cumsum(f * vib) / sr
        tone = (np.sin(ph) + 0.4 * np.sin(2 * ph) + 0.2 * np.sin(3 * ph))
        a = int(0.05 * sr)
        env = np.ones(m)
        env[:a] = np.linspace(0, 1, a)
        env[-a:] = np.linspace(1, 0, a)
        st = int(start * sr)
        buf[st:st + m] += tone * env * 0.5
    stereo = np.stack([buf, buf], axis=1)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return {"ok": True, "out_path": path, "duration_s": duration_s,
            "sample_rate": sr, "n_samples": n}


def _spectral_onsets(mono, sr, hop_s=0.02, k=3.0):
    hop = int(hop_s * sr)
    nfr = len(mono) // hop
    mags = np.abs(np.fft.rfft(
        mono[:nfr * hop].reshape(nfr, hop) * np.hanning(hop), axis=1))
    flux = np.maximum(0, np.diff(np.log1p(mags), axis=0)).sum(axis=1)
    thr = np.median(flux) + k * np.std(flux)
    return int((flux > thr).sum())


def test_synthesize_instrumental_8bar(tmp_path):
    out = str(tmp_path / "inst.wav")
    spec = {"tempo_bpm": 120.0, "key": "C",
            "chords": ["C", "G", "Am", "F"], "bars": 8, "style": "ballad"}
    res = synthesize_instrumental(spec, out)
    assert res["ok"] and res["out_path"] == out
    assert res["sample_rate"] == SAMPLE_RATE

    expected = 8 * 4 * 60.0 / 120.0 + 0.5
    assert os.path.exists(out)
    pcm, dur = _read_pcm(out)
    assert abs(dur - expected) < 0.5, f"duration {dur} vs {expected}"
    assert abs(res["duration_s"] - dur) < 0.01

    rms = float(np.sqrt((pcm ** 2).mean()))
    assert rms > 0.02, f"too quiet, rms={rms}"  # well above silence

    mono = pcm.mean(axis=1)
    # harmonic peaks at bar-0 C-major chord frequencies (130.81/164.81/196.00 Hz)
    bar0 = mono[:int(2.0 * SAMPLE_RATE)]
    spec_mag = np.abs(np.fft.rfft(bar0 * np.hanning(len(bar0))))
    freqs = np.fft.rfftfreq(len(bar0), 1.0 / SAMPLE_RATE)
    peaks, props = spsig.find_peaks(spec_mag, height=np.median(spec_mag) * 6,
                                    distance=int(3 / (freqs[1] - freqs[0])))
    peak_freqs = freqs[peaks]
    for target in (130.81, 164.81, 196.00):
        assert np.any(np.abs(peak_freqs - target) < 3.0), \
            f"no harmonic peak near {target} Hz (found {peak_freqs[:12]})"

    # percussion transients: onset count well above a handful
    onsets = _spectral_onsets(mono, SAMPLE_RATE)
    assert onsets > 20, f"only {onsets} onsets detected"


def test_synthesize_upbeat_style(tmp_path):
    out = str(tmp_path / "up.wav")
    res = synthesize_instrumental(
        {"tempo_bpm": 128.0, "key": "Am", "chords": ["Am", "F", "C", "G"],
         "bars": 4, "style": "upbeat", "seed": 3}, out)
    assert res["ok"]
    pcm, dur = _read_pcm(out)
    assert abs(dur - (4 * 4 * 60.0 / 128.0 + 0.5)) < 0.5
    assert float(np.sqrt((pcm ** 2).mean())) > 0.02
    assert _spectral_onsets(pcm.mean(axis=1), SAMPLE_RATE) > 10


def test_assemble_song_with_fixture_vocal(tmp_path, monkeypatch):
    work = str(tmp_path / "work")
    os.makedirs(work)
    fixture = str(tmp_path / FIXTURE_NAME)

    def fake_voice(text, wav_path, voice="default"):
        assert isinstance(text, str) and len(text) > 0
        return make_test_fixture_vocal(wav_path, duration_s=3.0)

    monkeypatch.setattr(song_mod, "_synthesize_voice", fake_voice)

    out = str(tmp_path / "song.wav")
    spec = {"tempo_bpm": 120.0, "key": "C",
            "chords": ["C", "G", "Am", "F"], "bars": 8, "style": "ballad"}
    res = assemble_song("la la la, this is a test song", spec, out, work)
    assert res["ok"], res
    assert os.path.exists(out)
    for k in ("out_path", "duration_s", "sample_rate", "instrumental_path",
              "vocal_path", "instrumental_duration_s", "vocal_duration_s"):
        assert k in res, f"missing key {k}"

    pcm, dur = _read_pcm(out)
    expect = max(res["instrumental_duration_s"], res["vocal_duration_s"])
    assert abs(dur - expect) < 0.5, f"song duration {dur} vs {expect}"
    assert dur > 15.0  # 8 bars at 120 bpm

    mono = pcm.mean(axis=1)
    sr = SAMPLE_RATE

    def band_energy(seg, lo=300.0, hi=3400.0):
        m = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        f = np.fft.rfftfreq(len(seg), 1.0 / sr)
        return float((m[(f >= lo) & (f <= hi)] ** 2).sum())

    vocal_seg = mono[int(0.2 * sr):int(2.8 * sr)]      # fixture active
    inst_only = mono[int(8 * sr):int(12 * sr)]         # instrumental only
    e_vocal = band_energy(vocal_seg)
    e_inst = band_energy(inst_only)
    assert e_vocal > 4.0 * e_inst, \
        f"vocal not audibly present: vocal-band {e_vocal:.3g} vs inst-only {e_inst:.3g}"

    # instrumental stem alive where the vocal is absent
    assert float(np.sqrt((inst_only ** 2).mean())) > 0.01
    assert _spectral_onsets(inst_only, sr) > 5


def test_assemble_song_fails_closed_on_bad_voice(tmp_path, monkeypatch):
    def bad_voice(text, wav_path, voice="default"):
        return {"ok": False, "error": "no voice substrate"}
    monkeypatch.setattr(song_mod, "_synthesize_voice", bad_voice)
    out = str(tmp_path / "song.wav")
    res = assemble_song("hello", {"bars": 2}, out, str(tmp_path / "w"))
    assert res["ok"] is False
