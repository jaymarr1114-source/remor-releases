#!/bin/bash
# ACQ-STT-1 gate battery: reproduces the whole acquisition proof in fresh
# processes. Exit 0 iff every check passes.
#
# Prerequisites (built by the mission, verified here, rebuilt if missing):
#   ~/workspace/scratch/acq-stt-1/venv      sandbox venv (faster-whisper 1.2.1)
#   ~/workspace/scratch/acq-stt-1/dl        hash-verified wheels
#   ~/workspace/models/faster-whisper-base  dual-verified checkpoint
#   ~/workspace/scratch/acq-stt-1/fixtures  piper-synthesized WER fixtures
#
# Usage: proofs/acqstt1/gate_run.sh
# Run from the acq-stt-1 worktree root.

set -u
WT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRATCH="$HOME/workspace/scratch/acq-stt-1"
MODEL_DIR="$HOME/workspace/models/faster-whisper-base"
VENV="$SCRATCH/venv"
DL="$SCRATCH/dl"
FIX="$SCRATCH/fixtures"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "PASS $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL $1 -- $2"; }

echo "=== ACQ-STT-1 gate ==="
echo "worktree: $WT"

# --- 1. wheel hash re-verification (governed fetch integrity) ---------------
echo "--- [1] wheel hashes vs PyPI published digests"
"$WT/../scratch/acq-stt-1/venv/bin/python" - 2>/dev/null || true
python3 - "$DL" <<'EOF'
import hashlib, json, os, sys, urllib.request
dl = sys.argv[1]
def pkg_ver(fn):
    p = fn[:-4].split('-') if fn.endswith('.whl') else None
    if p: return p[0].replace('_','-'), p[1]
    b = fn[:-7]; q = b.rsplit('-',1); return q[0].replace('_','-'), q[1]
n_ok = n_bad = 0
for fn in sorted(os.listdir(dl)):
    if not fn.endswith('.whl'): continue
    name, ver = pkg_ver(fn)
    if (name, ver) not in [('faster-whisper','1.2.1'),('av','14.1.0')]:
        continue  # mission pins; deps verified at acquisition time
    with open(os.path.join(dl, fn),'rb') as f:
        obs = hashlib.sha256(f.read()).hexdigest()
    with urllib.request.urlopen(
            f'https://pypi.org/pypi/{name}/{ver}/json', timeout=30) as r:
        data = json.load(r)
    pub = {u['filename']: u['digests']['sha256'] for u in data['urls']}
    if pub.get(fn, '').lower() == obs.lower():
        n_ok += 1
    else:
        n_bad += 1; print('MISMATCH', fn)
print(f'WHEELS_OK={n_ok} WHEELS_BAD={n_bad}')
sys.exit(0 if n_bad == 0 and n_ok == 2 else 1)
EOF
[ $? -eq 0 ] && ok "wheel hashes (faster-whisper 1.2.1, av 14.1.0)" \
             || bad "wheel hashes" "mismatch or unreachable"

# --- 2. checkpoint integrity -------------------------------------------------
echo "--- [2] checkpoint files vs manifest"
python3 - "$MODEL_DIR" <<'EOF'
import hashlib, json, os, sys
md = sys.argv[1]
man = json.load(open(os.path.join(md, 'checkpoint_manifest.json')))
assert man['revision'] == 'ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66', 'revision'
bad = []
for fn, meta in man['files'].items():
    h = hashlib.sha256(open(os.path.join(md, fn),'rb').read()).hexdigest()
    if h != meta['sha256']: bad.append(fn)
print('CHECKPOINT_BAD=' + str(len(bad)))
sys.exit(1 if bad else 0)
EOF
[ $? -eq 0 ] && ok "checkpoint integrity (4 files, pinned revision)" \
             || bad "checkpoint integrity" "hash mismatch"

# --- 3. WER battery (fresh process) ------------------------------------------
echo "--- [3] WER battery"
"$VENV/bin/python" "$WT/proofs/acqstt1/wer_battery.py" \
    "$MODEL_DIR" "$FIX" "$SCRATCH/wer_gate.json" > "$SCRATCH/wer_gate.log" 2>&1
[ $? -eq 0 ] && ok "WER battery (mean<0.20, silence clean)" \
             || bad "WER battery" "$(tail -3 $SCRATCH/wer_gate.log)"

# --- 4. negative control (pristine tree, fresh process) -----------------------
echo "--- [4] negative control"
PW="$HOME/workspace/worktrees/acq-stt-1-pristine"
[ -d "$PW" ] || git -C "$HOME/workspace/remor_convergence/canonical" \
    worktree add --detach "$PW" 3fe73a7 >/dev/null 2>&1
"$VENV/bin/python" - "$PW" <<'EOF'
import sys
pw = sys.argv[1]
sys.path.insert(0, pw + '/pylib'); sys.path.insert(0, pw)
from runtime.services import voice
from swarm_engine.services.unavailable import CapabilityUnavailable
try:
    voice.transcribe('/home/hatch/workspace/scratch/acq-stt-1/fixtures/fixture_0.wav')
    print('NEG_CONTROL=FAIL'); sys.exit(1)
except CapabilityUnavailable:
    print('NEG_CONTROL=PASS'); sys.exit(0)
EOF
[ $? -eq 0 ] && ok "negative control (pre-wire refuses)" \
             || bad "negative control" "pre-wire did not refuse"

# --- 5. route test (fresh process, live server) --------------------------------
echo "--- [5] route test"
"$VENV/bin/python" "$WT/proofs/acqstt1/route_test.py" \
    "$FIX/fixture_0.wav" > "$SCRATCH/route_gate.log" 2>&1
grep -q "0 FAIL" "$SCRATCH/route_gate.log" \
    && ok "route test (200 transcript / 400 / status)" \
    || bad "route test" "$(grep FAIL $SCRATCH/route_gate.log | head -3)"

# --- 6. catalog entry completeness ---------------------------------------------
echo "--- [6] catalog entry"
python3 - "$WT/proofs/acqstt1/catalog_entry.json" <<'EOF'
import json, sys
e = json.load(open(sys.argv[1]))
required = ['capability','provenance','license','version','dependencies',
            'validation_evidence','integrity_state','lifecycle_status']
missing = [k for k in required if k not in e]
assert not missing, f'missing fields: {missing}'
assert e['license']['wheel'].startswith('MIT'), 'wheel license'
assert e['validation_evidence']['wer_battery']['result'] == 'PASS'
print('CATALOG_OK')
EOF
[ $? -eq 0 ] && ok "catalog entry (all governance fields)" \
             || bad "catalog entry" "incomplete"

echo "=== gate: $PASS PASS, $FAIL FAIL ==="
[ "$FAIL" -eq 0 ]
