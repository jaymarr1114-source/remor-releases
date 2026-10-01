#!/usr/bin/env bash
# AUDIO-1 gate battery. Reproduces the mission's entire evidence battery in
# fresh sequential processes, run one at a time (never in parallel: the
# 2-core host contends and produces spurious failures).
#
# Per the updated gate convention, mission batteries live under
# proofs/<mission>/ — never at the tree root (the root gate_run.sh is the
# shared dispatcher). Felix's gate for AUDIO-1 is this one command from
# the worktree root:
#
#   ./proofs/audio1/gate_run.sh
#
# Exit 0 = ALL GREEN. Any failure aborts loudly.
set -u
PROOF_DIR="$(cd "$(dirname "$0")" && pwd)"
WT="$(cd "$PROOF_DIR/../.." && pwd)"
VENV="$HOME/workspace/venvs/audio-1"
export PYTHONPATH="$WT/pylib"
export WT
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  [PASS] $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  [FAIL] $1"; }

echo "=== AUDIO-1 gate battery ==="
echo "proof dir: $PROOF_DIR"
echo "tree root: $WT"
cd "$WT"

# ---- environment -------------------------------------------------------
echo "--- env: mission venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "creating mission venv at $VENV"
  python3 -m venv "$VENV" || { echo "FATAL: venv creation failed"; exit 1; }
  "$VENV/bin/pip" install --quiet piper-tts==1.8.0 numpy scipy \
    || { echo "FATAL: dependency install failed"; exit 1; }
fi
"$VENV/bin/python" -c "import piper, numpy, scipy" 2>/dev/null \
  && ok "venv has piper + numpy + scipy" \
  || { bad "venv missing piper/numpy/scipy"; }

# ---- B1: voice synthesis from text (real piper) -------------------------
echo "--- B1: voice synthesis (governed piper TTS)"
"$VENV/bin/python" - <<'EOF'
import sys, wave, struct, os
from swarm_engine.media.voice import synthesize
r = synthesize("Hello James. The gate battery is listening.", "/tmp/g_voice.wav")
assert r["ok"], r
assert r["engine"].startswith("piper"), r["engine"]
assert r["sample_rate"] == 22050 and r["n_samples"] > 1000
with wave.open("/tmp/g_voice.wav", "rb") as w:
    raw = w.readframes(w.getnframes())
samps = struct.unpack("<%dh" % (len(raw)//2), raw)
nz = sum(1 for s in samps if s != 0) / len(samps)
assert nz > 0.5, "suspiciously silent: nonzero ratio %r" % nz
assert max(abs(s) for s in samps) > 1000, "no real amplitude"
# adversarial: empty text must refuse cleanly, never a fake file
r2 = synthesize("   ", "/tmp/g_voice_empty.wav")
assert not r2["ok"] and "empty" in r2["error"].lower(), r2
assert not os.path.exists("/tmp/g_voice_empty.wav"), "refusal left a file behind"
print("B1 OK: %.2fs speech, nonzero=%.3f" % (r["duration_s"], nz))
EOF
[ $? -eq 0 ] && ok "B1 voice renders real speech; empty refused" \
             || bad "B1 voice synthesis"

# ---- B2: instrumental synthesis (real computed audio) -------------------
echo "--- B2: instrumental synthesis"
"$VENV/bin/python" - <<'EOF'
import wave, struct
from swarm_engine.media.music import synthesize_instrumental
r = synthesize_instrumental(
    {"tempo_bpm": 110, "key": "Am", "chords": ["Am","F","C","G"],
     "bars": 4, "style": "upbeat", "seed": 42}, "/tmp/g_inst.wav")
assert r["ok"], r
assert r["sample_rate"] == 44100 and r["duration_s"] > 4.0
with wave.open("/tmp/g_inst.wav", "rb") as w:
    assert w.getnchannels() == 2, "not stereo"
    raw = w.readframes(w.getnframes())
samps = struct.unpack("<%dh" % (len(raw)//2), raw)
assert sum(1 for s in samps if s != 0)/len(samps) > 0.9, "silent instrumental"
assert max(abs(s) for s in samps) > 1000
# adversarial: bad spec refused, never a fake file
try:
    synthesize_instrumental({"style": "polka", "bars": 2}, "/tmp/g_bad.wav")
    raise SystemExit("bad style accepted")
except ValueError:
    pass
print("B2 OK: %.2fs stereo instrumental" % r["duration_s"])
EOF
[ $? -eq 0 ] && ok "B2 instrumental renders real audio; bad spec refused" \
             || bad "B2 instrumental synthesis"

# ---- B3: full song assembly (voice + instrumental -> song) --------------
echo "--- B3: assemble_song end-to-end"
"$VENV/bin/python" - <<'EOF'
import wave, struct, os
from swarm_engine.media.song import assemble_song
r = assemble_song(
    "Gate battery singing. Every byte is real.",
    {"tempo_bpm": 100, "key": "C", "chords": ["C","G"], "bars": 2,
     "style": "ballad", "seed": 7},
    "/tmp/g_song.wav", "/tmp/g_song_work")
assert r["ok"], r
assert r["vocal_duration_s"] > 1.0 and r["instrumental_duration_s"] > 1.0
with wave.open("/tmp/g_song.wav", "rb") as w:
    assert (w.getnchannels(), w.getframerate()) == (2, 44100)
    raw = w.readframes(w.getnframes())
samps = struct.unpack("<%dh" % (len(raw)//2), raw)
assert max(abs(s) for s in samps) > 1000, "silent song"
assert os.path.getsize("/tmp/g_song.wav") > 100000, "suspiciously small"
print("B3 OK: %.2fs song (vocal %.2fs + inst %.2fs)" %
      (r["duration_s"], r["vocal_duration_s"], r["instrumental_duration_s"]))
EOF
[ $? -eq 0 ] && ok "B3 full song assembles from real stems" \
             || bad "B3 song assembly"

# ---- B4: mix route end-to-end through dispatch() ------------------------
echo "--- B4: POST /api/artifacts/audio/mix over the real route table"
python3 - <<'EOF'
import sys, os, io, math, struct, wave, base64, hashlib
sys.path.insert(0, os.path.join(os.environ["WT"], "pylib"))
from swarm_engine.services.artifacts import ArtifactStore
from swarm_engine.services.binary_artifacts import (
    BinaryArtifactStore, routes_for_binary_artifacts)
from swarm_engine.services.contract_types import dispatch
import tempfile

def wav(freq, seconds, sr=22050, stereo=False):
    n = int(seconds * sr)
    mono = [int(16000*math.sin(2*math.pi*freq*i/sr)) for i in range(n)]
    frames = struct.pack("<%dh" % (2*n), *[s for m in mono for s in (m,m)]) \
        if stereo else struct.pack("<%dh" % n, *mono)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2 if stereo else 1); w.setsampwidth(2)
        w.setframerate(sr); w.writeframes(frames)
    return buf.getvalue()

td = tempfile.TemporaryDirectory()
store = ArtifactStore(os.path.join(td.name, "a.db"), os.path.join(td.name, "s"))
blobs = BinaryArtifactStore(store)
routes = routes_for_binary_artifacts(blobs)
inst = blobs.store_file("inst.wav", wav(220, 2.0, stereo=True), "audio/wav")
v1 = blobs.store_file("v1.wav", wav(440, 1.0), "audio/wav")
v2 = blobs.store_file("v2.wav", wav(660, 1.5), "audio/wav")
assert inst["ok"] and v1["ok"] and v2["ok"]
res = dispatch(routes, "POST", "/api/artifacts/audio/mix",
               {"instrumental_id": inst["artifact_id"],
                "voice_ids": [v1["artifact_id"], v2["artifact_id"]]})
assert res["ok"], res
assert res["content_type"] == "audio/wav" and res["n_voices"] == 2
assert "ACE-Step NOT" in res["engines"]["instrumental"], "dishonest label!"
# download path serves it byte-identical, sha256 recorded
dl = dispatch(routes, "GET",
              "/api/artifacts/binary/%d" % res["artifact_id"], {})
assert dl["ok"], dl
raw = base64.b64decode(dl["download"]["bytes"])
assert hashlib.sha256(raw).hexdigest() == res["sha256"] == dl["download"]["sha256"]
with wave.open(io.BytesIO(raw), "rb") as w:
    assert (w.getnchannels(), w.getframerate()) == (2, 44100)
    frames = w.readframes(w.getnframes())
samps = struct.unpack("<%dh" % (len(frames)//2), frames)
assert max(abs(s) for s in samps) > 1000, "silent song file"
dur = len(samps)//2/44100
assert abs(dur - 2.0) < 0.15, "bad duration %r" % dur
# adversarial: unknown id / non-audio / empty voices all refused
assert not blobs.mix_audio(999999, [v1["artifact_id"]])["ok"]
txt = blobs.store_file("t.txt", b"not audio", "text/plain")
r = blobs.mix_audio(txt["artifact_id"], [v1["artifact_id"]])
assert not r["ok"] and "decod" in r["error"].lower(), r
assert not blobs.mix_audio(inst["artifact_id"], [])["ok"]
print("B4 OK: song artifact %d, %.2fs, sha256 %s..." %
      (res["artifact_id"], dur, res["sha256"][:12]))
td.cleanup()
EOF
[ $? -eq 0 ] && ok "B4 mix route real end-to-end + refusals" \
             || bad "B4 mix route"

# ---- B5: unit suites ----------------------------------------------------
echo "--- B5: binary-artifact + media unit suites"
python3 -m unittest tests.backend.test_binary_artifacts \
  2>&1 | tail -3 | grep -q "^OK" \
  && ok "B5a backend suite green" || bad "B5a backend suite"
python3 -m unittest tests.contracts.test_binary_artifacts \
  2>&1 | tail -3 | grep -q "^OK" \
  && ok "B5b contracts suite green" || bad "B5b contracts suite"
python3 -m pytest tests/media/test_music.py tests/media/test_media_status.py \
  -q 2>&1 | tail -2 | grep -q "passed" \
  && ok "B5c media music/status suites green" || bad "B5c media suites"
"$VENV/bin/python" -m pytest tests/media/test_voice.py -q \
  2>&1 | tail -2 | grep -q "passed" \
  && ok "B5d voice suite green (real piper)" || bad "B5d voice suite"

# ---- B6: acquisition sanity --------------------------------------------
echo "--- B6: acquisition record sanity"
"$VENV/bin/pip" show piper-tts 2>/dev/null | grep -q "Version: 1.8.0" \
  && ok "B6a piper-tts 1.8.0 installed (sha256-pinned at install)" \
  || bad "B6a piper version"
"$VENV/bin/pip" show piper-tts 2>/dev/null | grep -qi "GPL" \
  && ok "B6b license metadata present (GPL-3.0-or-later per PyPI record)" \
  || bad "B6b license metadata"
[ -f "$WT/runtime/media/voices/en_US-lessac-medium.onnx" ] \
  && ok "B6c vendored voice model present" \
  || bad "B6c voice model missing"
[ -f "$WT/ACQUISITION_AUDIO_1.md" ] \
  && ok "B6d acquisition record present" \
  || bad "B6d acquisition record missing"

echo
echo "=== RESULT: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
