#!/usr/bin/env bash
# CREATIVITY-RELEASE-1 gate battery: the full evidence battery in fresh
# sequential processes. One command, exit 0 on green.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT_ROOT="${WT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
export WT_ROOT
# swarm_engine import root (pylib/swarm_engine -> ../runtime, same code)
export PYTHONPATH="$WT_ROOT:$WT_ROOT/pylib"
cd "$SCRIPT_DIR"

echo "== b1: compile check =="
python3 -m py_compile "$WT_ROOT/runtime/creativity/release.py" \
    || { echo "[FAIL] compile"; exit 1; }
echo "[PASS] compile"

echo "== b2: frozen interface present =="
test -f "$HOME/workspace/tracks/creativity-executive/interfaces/GALLERY_ADMISSION_v1.md" \
    || { echo "[FAIL] interface file missing"; exit 1; }
echo "[PASS] GALLERY_ADMISSION_v1.md present"

echo "== b3: proof battery (fresh process) =="
python3 proof_release.py
rc=$?
if [ $rc -ne 0 ]; then
    echo "[FAIL] proof_release.py exit=$rc"
    exit 1
fi

echo "GATE_RUN_DONE fail=0"
