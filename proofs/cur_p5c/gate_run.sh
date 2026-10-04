#!/bin/bash
# CUR-P5C gate: relevance fencing in Primary Acceptance.
# Runs the mission's FULL evidence battery in fresh sequential processes.
# Exit 0 only if every check passes. Batteries run SEQUENTIALLY (never in
# parallel) on the 2-core host.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKTREE="$(cd "$ROOT/../.." && pwd)"
cd "$WORKTREE" || exit 1

echo "=== CUR-P5C gate_run.sh ==="
echo "--- 0. worktree pin (fail-closed, merge-base ancestry) ---"
PIN_BASE="84b2a8006e38f04a77f7b7290e8a7b0ca6c158f2"
if git merge-base --is-ancestor "$PIN_BASE" HEAD 2>/dev/null; then
  echo "PASS: worktree based at 84b2a80 (HEAD $(git rev-parse --short HEAD))"
else
  echo "FAIL: worktree not based at 84b2a80 -- refusing to run"
  exit 1
fi

echo "=== CUR-P5C battery ==="
rm -rf /tmp/cur_p5c_runs
python3 proofs/cur_p5c/cur_p5c_proof.py || exit 1
echo "=== CUR-P5C gate: ALL BATTERIES GREEN ==="
