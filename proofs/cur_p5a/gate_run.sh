#!/bin/bash
# CUR-P5A gate: curiosity-initiated work (C-1.3/C-1.4).
# Runs the mission's FULL evidence battery in fresh sequential processes.
# Exit 0 only if every check passes.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKTREE="$(cd "$ROOT/../.." && pwd)"
cd "$WORKTREE" || exit 1
PIN_BASE="84b2a8006e38f04a77f7b7290e8a7b0ca6c158f2"
echo "--- 0. worktree pin (fail-closed, merge-base ancestry) ---"
if git merge-base --is-ancestor "$PIN_BASE" HEAD; then
  echo "PASS: worktree based at 84b2a80 (HEAD $(git rev-parse --short HEAD))"
else
  echo "FAIL: worktree not based at 84b2a80 -- refusing to run"
  exit 1
fi
rm -rf proofs/cur_p5a/runs
echo "=== CUR-P5A battery ==="
python3 proofs/cur_p5a/cur_p5a_proof.py || exit 1
echo "=== CUR-P5A gate: ALL BATTERIES GREEN ==="
