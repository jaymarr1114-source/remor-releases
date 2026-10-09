#!/bin/bash
# ACQ-BRIDGE-1 gate battery. Reproduces the bound-bridge proof + negative
# control in fresh processes. Exit 0 only if every check passes.
#
# Usage: ./gate_run.sh  (run from the worktree root; or set WT_ROOT)
set -u
WT_ROOT="${WT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
export PYTHONPATH="$WT_ROOT/pylib"
PASS=0; FAIL=0

check() { # name, command...
  local name="$1"; shift
  echo "--- $name"
  if timeout 300 python3 "$@"; then
    echo "PASS: $name"; PASS=$((PASS+1))
  else
    echo "FAIL: $name (exit $?)"; FAIL=$((FAIL+1))
  fi
}

check "bridge_e2e (bound search + discovery + unbound refusal + honesty)" \
  "$WT_ROOT/proofs/acqbridge1/bridge_e2e.py"

echo "=== ACQ-BRIDGE-1 gate: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" -eq 0 ]
