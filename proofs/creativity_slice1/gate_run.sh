#!/bin/bash
# CREATIVITY-SLICE-1 gate_run.sh — reproduces the mission's full evidence
# battery in a fresh process. Felix's gate is one command from the
# worktree root:
#
#   ./proofs/creativity_slice1/gate_run.sh
#
# What it does:
#   1. Runs proofs/creativity_slice1/creativity_slice_proof.py (the decided
#      D-5 stages + D-4 intent register battery) in a fresh process.
#
# Scope (per CREATIVITY-SLICE-1): paper-specified D-4/D-5 slices only —
# no ledger, novelty check, critique machinery, composition search,
# arbitration, executive, run controller, or budgets. The build hold on
# the creativity executive remains; this gate touches no executive code.
#
# Exit 0 only if every battery passes.
set -u
TREE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$TREE" || exit 1
PASS=0; FAIL=0

check() { # $1=name $2=condition(0=pass)
  if [ "$2" -eq 0 ]; then echo "PASS: $1"; PASS=$((PASS+1));
  else echo "FAIL: $1"; FAIL=$((FAIL+1)); fi
}

# 1. Creativity slice battery in a fresh process
python3 proofs/creativity_slice1/creativity_slice_proof.py
check "creativity_slice_battery" $?

echo "==="
echo "gate: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
