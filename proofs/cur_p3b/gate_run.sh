#!/bin/bash
# CUR-P3B gate_run.sh -- reproduces the mission's entire evidence
# battery in fresh sequential processes. One invocation, exit 0 on
# green. No hardcoded home paths: the worktree root is this script's
# grandparent's parent (proofs/cur_p3b/gate_run.sh -> worktree).
set -u
WT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROOF="$WT_ROOT/proofs/cur_p3b/cur_p3b_proof.py"

echo "=== CUR-P3B gate_run.sh ==="
echo "--- 0. worktree pin (fail-closed) ---"
PIN="$(cd "$WT_ROOT" && git rev-parse HEAD)"
if [ "$PIN" = "0dd0e76ec7414426c273beb8bb91bd9b9b9234bd" ]; then
  echo "PASS: worktree pinned at 0dd0e76"
else
  echo "FAIL: worktree at $PIN, expected 0dd0e76 -- refusing to run"
  exit 1
fi

echo "--- 1. loop module imports clean ---"
python3 -c "
import sys; sys.path.insert(0, '$WT_ROOT/pylib')
import swarm_engine.curiosity.loops.creative_exploration as ce
print('PASS: imports clean')
" || { echo "FAIL: import"; exit 1; }

echo "--- 2. proof battery (fresh process) ---"
python3 "$PROOF"
code=$?
if [ $code -eq 0 ]; then
  echo "=== CUR-P3B gate: ALL BATTERIES GREEN ==="
else
  echo "=== CUR-P3B gate: BATTERY FAILED (exit $code) ==="
fi
exit $code
