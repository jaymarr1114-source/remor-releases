#!/bin/bash
# BRIDGE-BOOT-1 gate battery. Reproduces the boot-binding proof in fresh
# processes: real serve() boot -> bound executive -> activation fires the
# pipeline -> honest no-candidate; plus boot fail-safe and unbound-refusal
# negative controls. Exit 0 only if every check passes.
#
# Usage: ./gate_run.sh  (run from the worktree root; or set WT_ROOT)
set -u
WT_ROOT="${WT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
export PYTHONPATH="$WT_ROOT/pylib:$WT_ROOT/distill1:$WT_ROOT"
PASS=0; FAIL=0

check() { # name, command...
  local name="$1"; shift
  echo "--- $name"
  if timeout 600 python3 "$@"; then
    echo "PASS: $name"; PASS=$((PASS+1))
  else
    echo "FAIL: $name (exit $?)"; FAIL=$((FAIL+1))
  fi
}

check "boot_e2e (boot wiring + pipeline fire + fail-safe + refusal controls)" \
  "$WT_ROOT/proofs/bridgeboot1/boot_e2e.py"

echo "=== BRIDGE-BOOT-1 gate: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" -eq 0 ]
