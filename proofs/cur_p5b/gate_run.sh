#!/bin/bash
# CUR-P5B gate: first-pass triage (C-3.3/C-3.4).
# Runs the mission's FULL evidence battery in fresh sequential
# processes. Batteries run SEQUENTIALLY (never in parallel) on the
# 2-core host. Exit 0 only if every check passes.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKTREE="$(cd "$ROOT/../.." && pwd)"
cd "$WORKTREE" || exit 1

echo "=== CUR-P5B gate_run.sh ==="
echo "--- 0. worktree pin (fail-closed, merge-base ancestry) ---"
PIN_BASE="84b2a8006e38f04a77f7b7290e8a7b0ca6c158f2"
if git merge-base --is-ancestor "$PIN_BASE" HEAD; then
  echo "PASS: worktree based at 84b2a80 (HEAD $(git rev-parse --short HEAD))"
else
  echo "FAIL: worktree not based at 84b2a80 -- refusing to run"
  exit 1
fi

rm -rf proofs/cur_p5b/runs
echo "=== CUR-P5B battery ==="
python3 proofs/cur_p5b/cur_p5b_proof.py || exit 1
echo "=== CUR-P5B gate: ALL BATTERIES GREEN ==="
