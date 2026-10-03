#!/usr/bin/env bash
# CREATIVITY-CRITIQUE-1 gate battery: the full evidence battery in fresh
# sequential processes. One command, exit 0 on green.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT_ROOT="${WT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
export WT_ROOT
# swarm_engine import root (pylib/swarm_engine -> ../runtime, same code)
export PYTHONPATH="$WT_ROOT:$WT_ROOT/pylib"
cd "$SCRIPT_DIR"

echo "== b1: compile check =="
python3 -m py_compile "$WT_ROOT/runtime/creativity/critique.py" \
    || { echo "[FAIL] compile"; exit 1; }
echo "[PASS] compile"

echo "== b2: proof battery (fresh process) =="
python3 proof_critique.py
rc=$?
if [ $rc -ne 0 ]; then
    echo "[FAIL] proof_critique.py exit=$rc"
    exit 1
fi

echo "GATE_RUN_DONE fail=0"
